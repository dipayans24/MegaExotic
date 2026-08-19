from datetime import date, datetime, timedelta
import glob
import io
import os
import re
import tempfile
import warnings

import gdown
import gspread
import pandas as pd
import streamlit as st
from stqdm import stqdm
 
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from gspread_dataframe import get_as_dataframe, set_with_dataframe
from oauth2client.service_account import ServiceAccountCredentials

warnings.filterwarnings('ignore')


SCOPES = ['https://www.googleapis.com/auth/drive']


def save_upload(fileupload, fileType = None):
    temp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(temp_dir, fileupload.name)

    with open(tmp_path, "wb") as f:
        f.write(fileupload.getvalue())
   
    return tmp_path

def next_sunday():
    """
    Returns the date of the upcoming Sunday in 'YYYY-MM-DD' format.
    If today is already a Sunday, returns today's date.
    """
    today = date.today()
    # Monday=0 ... Sunday=6
    days_until_sunday = (6 - today.weekday()) % 7
    result = today + timedelta(days=days_until_sunday)
    return result.strftime("%Y-%m-%d")

def getGdriveService(GdriveCredentials, delegated_user=None):
    # Authenticates with Google Drive using a service account file
    # Pass delegated_user="someone@yourdomain.com" to impersonate a real user (needed if
    # uploading/downloading against a personal My Drive folder rather than a Shared Drive)

    creds = service_account.Credentials.from_service_account_file(GdriveCredentials, scopes=SCOPES)

    if delegated_user:
        creds = creds.with_subject(delegated_user)

    return build('drive', 'v3', credentials=creds)

def getFilesList(parent_folder_id, service):
    # Retrieves ALL files/folders within a parent folder (paginated, Shared-Drive aware)
    file_list = []
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{parent_folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora='allDrives'
        ).execute()
        file_list.extend(results.get('files', []))
        page_token = results.get('nextPageToken')
        if not page_token:
            break
    return file_list

def getSubfolderId(parent_folder_id, folder_name, service):
    # Looks up a named subfolder's ID within a parent folder
    for item in getFilesList(parent_folder_id, service):
        if item['name'] == folder_name:
            return item['id']
    return None

# ---------- Download ----------

def download_file(service, file_id, file_name, clear):
    # Downloads a single file straight to disk, with a Streamlit progress bar
    if file_name not in st.session_state or clear:
        
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

        progress_bar = st.progress(0, text=f"Downloading {file_name}...")

        with open(file_name, 'wb') as f:
            downloader = MediaIoBaseDownload(fd=f, request=request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    progress_bar.progress(pct, text=f"Downloading {file_name}... {pct}%")

        progress_bar.progress(100, text=f"{file_name} downloaded")

        st.session_state[file_name] = file_name
        return file_name

    else:
        return st.session_state[file_name]

def getFilefromGdrive(folder_id, service, ProcessParameter, clear):
    # Downloads all files from a named subfolder within folder_id
    subfolder_id = getSubfolderId(folder_id, ProcessParameter, service)
    file_list = getFilesList(subfolder_id, service)

    filePaths = []
    for f in  file_list :
        download_file(service, f['id'], f['name'], clear)
        filePaths.append(f['name'])

    return filePaths, service


def getSheet(sheet_id, sheet_name, credential_Upload):
  scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
  creds = ServiceAccountCredentials.from_json_keyfile_name(credential_Upload, scope)
  client = gspread.authorize(creds)

  try:
        workbook = client.open_by_key(sheet_id)
        values = workbook.worksheet(sheet_name).get_all_values()
        records = workbook.worksheet(sheet_name).get_all_records()
        # sheet_name = datetime.now().strftime("%b-%Y")

  # Read the downloaded XLSX file into a pandas DataFrame
  
        paymentSlugs = pd.DataFrame(values[1:], columns=values[0])
        return paymentSlugs
  except:
      return None

#Pre-Processing Payment Report and getDates Functions
def generatePaymentReport(filePath, paymentSlugs):
  paymentReport = pd.read_csv(filePath[0], sep=",", date_format="%Y-%m-%d %H:%M:%S", dayfirst=True,  low_memory=False) # Load data

  paymentReport = paymentReport[(paymentReport["Tags"].fillna("empty").str.contains("l1") | paymentReport["Tags"].isna() )] # Filter by tags

  paymentReport["Phone Number"] = paymentReport["Phone Number"].astype(str).str.replace(r"\D", "", regex=True) # Sanitize numbers

  paymentReport = paymentReport[(paymentReport["Status"].str.strip().str.lower()  == "captured")] # Filter captured status

  paymentReport["CreatedAt"] = pd.to_datetime(paymentReport["CreatedAt"], format = "%Y-%m-%d %H:%M:%S", exact=True, dayfirst=True, yearfirst=False) # Format dates

  paymentSlugs["PaymentFunnel"] = paymentSlugs["PaymentFunnel"].apply(lambda x: pd.NA if x  == '' else x) # Handle empty strings
  paymentSlugs.dropna(subset = ["PaymentFunnel"], inplace = True, how="any") # Drop missing funnels

  paymentReport = paymentReport.merge(paymentSlugs[(paymentSlugs["isExotic"] == "Yes")],  right_on="ID", left_on = "Payment Slug", how="left") # Merge with slugs

  Unmatched_Slugs = paymentReport[paymentReport["PaymentFunnel"].isna()] # Identify unmatched records
  Unmatched_Slugs.to_csv("UnmatchedExotic_Slugs.csv", index=False, sep=",") # Save orphans

  SlugsList = sorted(paymentSlugs["PaymentFunnel"].unique().tolist()) # List all funnels
  paymentReport = paymentReport[~paymentReport["PaymentFunnel"].isna()] # Keep only matched

  paymentReport.drop(columns= "isExotic", inplace=True) # Cleanup columns

  return paymentReport

def getDates(Funnel):

  BatchDate[["Date", 'StartDate', 'EndDate']] = BatchDate[["Date", 'StartDate', 'EndDate']]#.astype('M8[s]')
  ExcludedTimings[["Date", 'StartDate', 'EndDate']] = ExcludedTimings[["Date", 'StartDate', 'EndDate']]#.astype('M8[s]')

  FilteredBatchDate = BatchDate[(BatchDate["Date"] == WSDate) & (BatchDate["Funnel"]  == Funnel) ]

  if len(FilteredBatchDate)>0:
    startDate = FilteredBatchDate["StartDate"].iloc[0]
    endDate = FilteredBatchDate["EndDate"].iloc[0]
  else:
    startDate = None
    endDate = None

  FilteredExcludedTimings = ExcludedTimings[(ExcludedTimings["Date"] == WSDate) & (ExcludedTimings["Funnel"]  == Funnel)]

  if len(FilteredExcludedTimings) > 0:
    excludedStartDates = FilteredExcludedTimings["StartDate"]
    excludedEndDates = FilteredExcludedTimings["EndDate"]
  else:
    excludedStartDates = None
    excludedEndDates = None

  return startDate, endDate, excludedStartDates, excludedEndDates

def CountIf(Main_File, Current_File, MFCol, CFCol, filename):

    MainFileColCleaned = MFCol.replace(" ", "") # Clean whitespace from main column name
    CurrentFileColCleaned = CFCol.replace(" ", "") # Clean whitespace from current column name

    NewColName = MainFileColCleaned[:5]+"_"+re.sub(r"\W", "", filename[:5])+"_"+CurrentFileColCleaned # Construct unique column name

    newcol = 1 # Initialize suffix counter
    while NewColName in Main_File.columns:
        NewColName = NewColName+"_"+str(newcol) # Append suffix if name exists
        newcol = newcol+1 # Increment counter

    lookup_set = set(Current_File[CFCol].astype(str).str.lower().str.strip()) # Create optimized lookup set
    Main_File.insert(loc=len(Main_File.columns), column=NewColName,
                     value=Main_File[MFCol].astype(str).str.lower().str.strip().isin(lookup_set).astype(int),
                     allow_duplicates=True) # Insert binary match column

    return Main_File, NewColName # Return modified dataframe and name

#  Generate the MEGA Report

def processMEGA(Funnels, filePath, InfoDataPath, getSheets, sheet_id, ExcludeAmount,paymentSlugs,  credential_Upload):
  InfoData = pd.read_csv(InfoDataPath[0], sep=",", low_memory=False) # Load lead info
  InfoData.rename(columns = {"email" :"Email", "phone_number" : "Phone Number"}, inplace=True) # Standardize headers
  InfoData["Phone Number"] = InfoData["Phone Number"].astype(str).str.replace(r"\D", "", regex=True).str.strip() # Clean phone strings

  FileList = [] # Log successful files
  ExcludedData = [] # Log excluded files

  FunnelCount = pd.DataFrame(columns = ["Funnel", "Count"]) # Init summary DF

  RawExoticLeads = generatePaymentReport(filePath, paymentSlugs) # Process input files

  for Funnel in stqdm(Funnels): # Process each funnel type
    data = pd.DataFrame() # Storage for excluded rows

    ExoticLeads = RawExoticLeads # Set working copy
    sheet_name = getSheets.get(Funnel) # Get sheet list for funnel

    NonExoticSheet = pd.DataFrame() # Storage for non-exotic data
    for sheet in sheet_name: # Load multiple sheets

        sheetDownload = getSheet(sheet_id, sheet, credential_Upload) # Fetch sheet data
        if sheetDownload is not None: # Confirm load
            NonExoticSheet = pd.concat([NonExoticSheet, sheetDownload], axis="rows", ignore_index=True) # Append

    st.write(f"TotalCount of {sheet} - {len(NonExoticSheet)}.") # Report sheet size

    startDate, endDate, excludedStartDates, excludedEndDates = getDates(Funnel) # Fetch time filters

    if startDate is not None: # Apply time range
        ExoticLeads = ExoticLeads[(ExoticLeads["PaymentFunnel"] == Funnel) & (ExoticLeads["CreatedAt"].between(startDate, endDate) )] # Filter by date
    else: # Handle missing dates
        ExoticLeads = ExoticLeads[(ExoticLeads["PaymentFunnel"] == Funnel)] # Filter by funnel only
        st.write(f"Start Date not defined for {Funnel}.") # Warn user

    ExoticLeads["Abandon Cart"] = "No" # Default flag

    if excludedStartDates is not None: # Apply specific exclusions
        excludedStartDatesTZ = [pd.to_datetime(d) for d in excludedStartDates] # Prep start times
        excludedEndDatesTZ   = [pd.to_datetime(d) for d in excludedEndDates] # Prep end times
        st.write(f"Number of date count(s) to be exluded - {len(excludedStartDates)}.") # Log count

        for start, end in zip(excludedStartDatesTZ, excludedEndDatesTZ): # Iterate ranges
            df =  ExoticLeads[ExoticLeads["CreatedAt"].between(start, end, inclusive="both")] # Get subset
            data = pd.concat([data, df], axis= "rows") # Track excluded
            ExoticLeads = ExoticLeads[~ExoticLeads["Payment Id"].isin(df["Payment Id"])] # Remove from main

        st.write(f"Count of excluded rows {len(data)}.") # Final log

    if Funnel in ExcludeAmount.keys(): # Price filtering
        FunnelPayment = FunnelPayment[~FunnelPayment["Amount"].isin(ExcludeAmount[Funnel])] # Remove specific amounts

    MFCombinations = ['Email', 'Phone Number'] # Matching columns
    NonExoticSheetCombinations = ['Email',  'Phone Number'] # Sheet columns

    SumColNames = [] # Aggregation log

    NonExoticSheet["Phone Number"] = NonExoticSheet["Phone Number"].astype(str) # Cast to string
    ExoticLeads["Phone Number"] = ExoticLeads["Phone Number"].astype(str) # Cast to string

    CurrentFileSumColumns = [] # Local sum tracking
    for MFCol, CFCol  in zip(MFCombinations, NonExoticSheetCombinations): # Loop matches
        ExoticLeads, NewColName = CountIf(ExoticLeads, NonExoticSheet, MFCol, CFCol, Funnel) # Check overlap
        SumColNames.append(NewColName) # Log column
        CurrentFileSumColumns.append(NewColName) # Log column

    CurrentDateColName = "Total" # Result column

    ExoticLeads[CurrentDateColName] = ExoticLeads[CurrentFileSumColumns].sum(axis=1).gt(0).map({True: 'Matched', False: 'Unmatched'}) # Logic for matching
    ExoticLeads = ExoticLeads[ExoticLeads[CurrentDateColName] == "Unmatched"] # Keep new only

    ExoticLeads.drop(columns=CurrentFileSumColumns+[CurrentDateColName, "ID"], inplace=True) # Cleanup

    ExoticLeads.rename(columns={"Payment Slug_y": "ExoticSlugs", "Payment Slug_x": "Payment Slug"}, inplace=True) # Fix naming

    ExoticLeads = ExoticLeads.sort_values(by=["Amount"], ascending=False) # Rank by value

    ExoticLeads["EmailLC"] = ExoticLeads["Email"].str.lower().str.strip() # Prep for unique check
    ExoticLeads.drop_duplicates(subset=["EmailLC", "Phone Number"],inplace=True, ignore_index=True) # Dedup combined
    ExoticLeads.drop_duplicates(subset=["EmailLC"], keep="first", inplace = True, ignore_index=True) # Dedup email
    ExoticLeads.drop_duplicates(subset=["Phone Number"], keep="first", inplace = True, ignore_index=True) # Dedup phone

    ExoticLeads.drop(columns=["EmailLC"], inplace=True) # Remove helper

    FunnelCount = pd.concat([FunnelCount, pd.DataFrame({"Funnel": [Funnel], "Count": [len(ExoticLeads)]})], axis="rows", ignore_index=True) # Update totals

    #st.write(f"{Funnel} count = {len(ExoticLeads)}.") # Log size

    columns = ["PaymentFunnel" , "Payment Id", "Payment Method", "Amount", "Email", "Phone Number", "Payment Slug", "ExoticSlugs", "Status", "Tags", "CreatedAt", "Source", "woocommerce OrderID", "Age Group", "Customer Name", "Business", "Profession (LSQ)", "Profession (PG)", "Abandon Cart"] # Selection

    ExoticLeads = ExoticLeads[columns] # Slice columns

    ExoticLeads = ExoticLeads.sort_values(by=["CreatedAt"], ascending=True) # Chronological sort

    ExoticLeads["Phone Number"] = ( # Re-sanitize final list
        ExoticLeads["Phone Number"].astype(str).str.replace(r"\D", "", regex=True).str.strip()
    )

    email_lookup = InfoData[["Email", "current_profession", "experience_in_years", "age_group"]].drop_duplicates("Email") # Build email cache
    phone_lookup = InfoData[["Phone Number", "current_profession", "experience_in_years", "age_group"]].drop_duplicates("Phone Number") # Build phone cache

    ExoticLeads = ExoticLeads.merge(email_lookup, on="Email", how="left", suffixes=("", "_email")) # Email enrichment
    ExoticLeads = ExoticLeads.merge(phone_lookup, on="Phone Number", how="left", suffixes=("", "_phone")) # Phone enrichment

    for col in ["current_profession", "experience_in_years", "age_group"]: # Coalesce results
        ExoticLeads[col] = ExoticLeads[col].combine_first(ExoticLeads[f"{col}_phone"]) # Fill missing
        ExoticLeads.drop(columns=[f"{col}_phone"], inplace=True) # Cleanup

    if len(ExoticLeads) > 0: # Save valid results
        output_filename = f"{Funnel}_{WSDate}.csv" # Set filename
        FileList.append(output_filename) # Log file
        ExoticLeads.to_csv(output_filename, index=False, sep=",") # Export

    if len(data) > 0: # Save exclusions
        output_filename = f"ExcludedData{Funnel}_.csv" # Set filename
        ExcludedData.append(output_filename) # Log file
        data = data.sort_values(by=["CreatedAt"], ascending=True) # Chrono sort
        data.to_csv(output_filename, index=False, sep=",") # Export

  Unmatched_Slugs = pd.read_csv("UnmatchedExotic_Slugs.csv") # Reload Orphans

  if len(Unmatched_Slugs) > 0:
     Unmatched_SlugsDF = Unmatched_Slugs.loc[Unmatched_Slugs["Payment Slug_x"].str.len() >= 36, "Payment Slug_x"].drop_duplicates()
  else:
     Unmatched_SlugsDF = None
   
    # @title Remove Duplicates between AI and AI BootcampPaid
  has_ai_exotic = any(kw.startswith("AI Exotic_") for kw in FileList)
  has_bootcamp = any(kw.startswith("AI Bootcamp_") for kw in FileList)

  if has_ai_exotic and has_bootcamp: # Run only if both exist
        AIExoticFilePath = [i for i in FileList if i.startswith("AI Exotic_")][0] # Locate file 1
        AIBootcampFilePath = [i for i in FileList if i.startswith("AI Bootcamp_")][0] # Locate file 2
        AIExotic = pd.read_csv(AIExoticFilePath) # Load file 1
        AIBootcamp = pd.read_csv(AIBootcampFilePath) # Load file 2

        MFCombinations = ['Email', 'Phone Number'] # Search keys
        AIBootcampSheetCombinations = ['Email',  'Phone Number'] # Target keys

        SumColNames = [] # Local log

        AIBootcamp["Phone Number"] = AIBootcamp["Phone Number"].astype(str) # String cast
        AIExotic["Phone Number"] = AIExotic["Phone Number"].astype(str) # String cast

        CurrentFileSumColumns = [] # Tracker for results
        for MFCol, CFCol  in zip(MFCombinations, AIBootcampSheetCombinations): # Loop overlap check
            AIExotic, NewColName = CountIf(AIExotic, AIBootcamp, MFCol, CFCol, Funnel) # cross-match
            SumColNames.append(NewColName) # Log
            CurrentFileSumColumns.append(NewColName) # Log

        TotalColName = "Total" # Flag column

        AIExotic[TotalColName] = AIExotic[CurrentFileSumColumns].sum(axis=1).gt(0).map({True: 'Matched', False: 'Unmatched'}) # Determine duplicates

        st.write(f"Dupliactes Count - {len(AIExotic[AIExotic[TotalColName] == "Matched"])}") # Log duplicate count

        AIExotic = AIExotic[AIExotic[TotalColName] == "Unmatched"] # Keep unique only

        FunnelCount.loc[(FunnelCount["Funnel"]=="AI Exotic"), "Count" ] = len(AIExotic) # Correct totals
        AIExotic.drop(columns=CurrentFileSumColumns+[TotalColName], inplace=True) # Cleanup columns

        AIExotic.rename(columns={"Payment Slug_y": "ExoticSlugs", "Payment Slug_x": "Payment Slug"}, inplace=True) # Restore names

        if len(AIExotic) > 0: # Save if not empty
            output_filename = f"AI Exotic_{WSDate}.csv" # Generate name
            AIExotic.to_csv(output_filename, index=False, sep=",") # Overwrite with cleaned data
            st.write(f"AI Exotic count = {len(AIExotic)}.") # Log final size

  return FileList, ExcludedData, FunnelCount, Unmatched_SlugsDF

def updateMegaSheet(credential_Upload, sheet_id, file):
  # Authentication
  scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
  creds = ServiceAccountCredentials.from_json_keyfile_name(credential_Upload, scope)
  client = gspread.authorize(creds)

  workbook = client.open_by_key(sheet_id)

  try:  #Gets the batchname by removing splitting from the "W" part.
        df = pd.read_csv(file, sep=",")
        columns = columns = ["CreatedAt","Customer Name", "Email", "Phone Number", "Amount", "Payment Slug", "ExoticSlugs", "Abandon Cart", 
                             "current_profession", "experience_in_years", "age_group"]
        MainFileBatches = file.split(".")[0].replace(f"_{WSDate}", "")
        df = df[columns]
  except:
      MainFileBatches = None

  existing_sheet_titles = [ws.title for ws in workbook.worksheets()]
  #st.write(existing_sheet_titles)

  if MainFileBatches is not None:
    if MainFileBatches not in existing_sheet_titles:
        AddNewWS = workbook.add_worksheet(title=MainFileBatches, rows='100', cols='20')
        batchData = AddNewWS # Use the newly created worksheet object
    else:
        batchData = workbook.worksheet(MainFileBatches)
        batchData.clear()

  # Ensure set_with_dataframe receives the worksheet object
    set_with_dataframe(batchData, df)
    os.unlink(file)
    st.write(f"Process Completed for {file}.")
    return True

  else:
    st.write(file)

def intilizeUpload(TotalFiles, sheet_id, credential_Upload):
    with st.status("Uploading..", expanded=True) as status:
        for file in stqdm(sorted(TotalFiles)):
            updateMegaSheet(credential_Upload, sheet_id, file)

        status.update(label="Upload Complete!!",expanded=False)

    st.success("Upload Completed!")

    st.session_state["upload_done"] = True  # mark upload as complete
    st.rerun()

def check_session_state(sheet_id  ,sessionVarName , sheet_name , credential_Upload , clear):
        if sessionVarName not in st.session_state or clear:
            st.write(f"Downloading {sessionVarName}.. ")
            st.session_state[sessionVarName] = getSheet( sheet_id, sheet_name, credential_Upload)
            return st.session_state[sessionVarName]
        else:
            return  st.session_state[sessionVarName]
    

st.set_page_config("📊 MEGA Sheet - Exotic", layout="wide")
st.header("📊 MEGA Sheet - Exotic", divider=True, text_alignment="center")
WSDate =  str(st.date_input("Select the Next Sunday date",value=next_sunday()))
credential_Upload = st.file_uploader("Upload Credentials File", type = ["json"]) 
GdriveCredentials =  st.file_uploader("Upload GDrive File", type = ["json"]) 
Funnels = st.multiselect(label="Select the Funnels", options=["AI Exotic", "SMAI Exotic", "PU Exotic", "AI Bootcamp"])

col1, col2, col3 = st.columns(3)    
with col1:
    download = st.checkbox("Download MEGA", persist_state="page", key="downloadKey")

with col2:
    IncludeExcludeData = st.checkbox("Include Excluded Data?")

with col3:
   clearPreviousData = st.checkbox("Clear Data?")

if WSDate and Funnels and GdriveCredentials and credential_Upload:
    genbtn = st.button("Generate Data", type="primary", on_click=None )

# Example usage:
    if genbtn:
        if "TotalFiles" in st.session_state:
            st.session_state.pop("TotalFiles") 

        if "upload_done" in st.session_state:
            st.session_state.pop("upload_done") 

        credential_Upload = save_upload(credential_Upload)
        st.session_state["credential_Upload"] = credential_Upload

        getSheets = {"AI Exotic": ["AI", "AI BootcampPaid"], "SMAI Exotic": ["SMAI"], "PU Exotic": ["PU"], "AI Bootcamp":[ "AI","AI BootcampPaid" ]} 

        GdriveCredentials = save_upload(GdriveCredentials)

        with st.status("Processing..", expanded=True) as status:
            service = getGdriveService(GdriveCredentials)  # or getGdriveService(delegated_user="owner@yourdomain.com")
            filePath, service = getFilefromGdrive('0AHGO663tIOm5Uk9PVA', service, WSDate, clearPreviousData)
            InfoDataPath, service = getFilefromGdrive('0AHH0Svj1my00Uk9PVA', service, WSDate,   clearPreviousData)
        # @title Downloading All Sheets
        # Remove existing file if it exists to avoid conflicts
        
            paymentSlugs = check_session_state("1v0UI5B4rkWJm3N8cbqnRCa4olvwV6-h-YC2mafNYnjU","paymentSlugs", WSDate, credential_Upload, clearPreviousData) 
 
            BatchDate = check_session_state("1szfXpbxy1lITxMU53e0TqlV_PjRVGv3OKTpI1wjoegk", "BatchDate", "BatchDate", credential_Upload, clearPreviousData)
 
            ExcludedTimings = check_session_state("1szfXpbxy1lITxMU53e0TqlV_PjRVGv3OKTpI1wjoegk", "ExcludedTimings", "ExcludedTimings", credential_Upload, clearPreviousData)
 
            MegaSheetInfo = check_session_state("1szfXpbxy1lITxMU53e0TqlV_PjRVGv3OKTpI1wjoegk", "MegaSheetInfo", "MegaSheetInfo", credential_Upload, clearPreviousData)
 
            ExcludeAmount = check_session_state("1szfXpbxy1lITxMU53e0TqlV_PjRVGv3OKTpI1wjoegk", "ExcludeAmount", "ExcludedAmount", credential_Upload, clearPreviousData) # Fetch amounts to exclude

            EA = ExcludeAmount.groupby("Funnel").apply(lambda x:  x["Amount"].astype(float).unique()).reset_index() # Group and find unique float amounts
            EA.columns = ["Funnel", "Amount"] # Rename columns
            ExcludeAmount = EA.set_index("Funnel")["Amount"].to_dict() # Convert to lookup dictionary

            condition = ((MegaSheetInfo["Date"] == WSDate) & (MegaSheetInfo["InUse"] == "Yes") )
            sheet_id = MegaSheetInfo.loc[condition,  "sheet_id" ].unique()[0]

            st.session_state["sheet_id"] = sheet_id

            status.update(label="Completed!",expanded=False)

            FileList, ExcludedData, FunnelCount, Unmatched_SlugsDF = processMEGA(Funnels,filePath,InfoDataPath,getSheets,  sheet_id, ExcludeAmount, paymentSlugs, credential_Upload)

        st.dataframe(MegaSheetInfo.loc[condition, ["Date", "sheet_id"]] , hide_index=True)

        if Unmatched_SlugsDF is not None or len(Unmatched_SlugsDF)>0:
          st.dataframe(Unmatched_SlugsDF,  hide_index=True)
         
        st.dataframe(FunnelCount, hide_index=True)
        
        TotalFiles = FileList+ExcludedData if IncludeExcludeData is True else FileList

        st.session_state["TotalFiles"] = TotalFiles

        if download:
            MegaFileName = rf"MegaExotic_{WSDate}.xlsx"
             
            requiredData = TotalFiles

            buffer = io.BytesIO()
            with pd.ExcelWriter(MegaFileName, engine="xlsxwriter") as f:
                for file in stqdm(sorted(requiredData), desc="Building MEGA file"):
                    data = pd.read_csv(file)
                    data["CreatedAt"] = data["CreatedAt"].astype('M8[s]').dt.strftime("%Y-%m-%d %H:%M:%S")
                    data.to_excel(f, sheet_name=file.split("_")[0], index=False)

                FunnelCount.to_excel(f, sheet_name="FunnelCount", index=False)

            with open(MegaFileName, "rb") as f:
                st.download_button(
                    label="Save MEGA file",
                    data=f.read(),
                    file_name=MegaFileName,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    on_click="ignore"
                )
    #Unmatched_Slugs.to_excel(f, sheet_name="Unmatched_Slugs", index=False)
    
        st.info(sheet_id)

    if "TotalFiles" in st.session_state:
        # upload = st.button("Upload Data?", type="primary", key= "upload" )

        # TotalFiles = st.session_state["TotalFiles"]
        # sheet_id = st.session_state["sheet_id"]
        # credential_Upload = st.session_state["credential_Upload"]
        # if upload:
        #     intilizeUpload(TotalFiles, sheet_id, credential_Upload) 

        if "upload_done" not in st.session_state:
            st.session_state["upload_done"] = False

        TotalFiles = st.session_state["TotalFiles"]
        sheet_id = st.session_state["sheet_id"]
        credential_Upload = st.session_state["credential_Upload"]

        if not st.session_state["upload_done"]:
            upload = st.button("Upload Data?", type="primary", key="upload")
            if upload:
                intilizeUpload(TotalFiles, sheet_id, credential_Upload)
        else:
            st.success("Upload Completed!")

else:
    with st.status("Credentials", expanded=False):
        col1, col2, col3, col4, col5  = st.columns(5, vertical_alignment = "center",  width="stretch") 
     
        with col1:
           st.link_button("Open 10xStats", "https://10xstats.com/", width  = "stretch")
        with col2:
           st.link_button("Open DirectUS", "https://directus-production-62b2.up.railway.app/admin/users/", width  = "stretch") 
        with col3:
           st.link_button("Open MEGA AC", "https://megaac.streamlit.app/", width  = "stretch") 
        with col4:
           st.link_button("Open MEGA Main", "https://megamain.streamlit.app/", width  = "stretch") 
        with col5:
           st.link_button("Open GdriveUpload", "https://gdriveupload.streamlit.app/", width  = "stretch")
         
        st.code("vivek.tiwari@houseofedtech.in")
        st.code("Sasaram@#1234")
        



