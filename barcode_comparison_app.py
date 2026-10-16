import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta
from azure.data.tables import TableClient
from collections import Counter
import json
import os
import re
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:58531").rstrip("/")
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN", "")
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_STORAGE_SAS_TOKEN = os.getenv("AZURE_STORAGE_SAS_TOKEN", "")
AZURE_TABLE_NAME = os.getenv("AZURE_TABLE_NAME", "BarcodeScanResults")

# Split PDF names look like abc_startpage_endpage.pdf (e.g. Booklet_21615575_1_7.pdf)
SPLIT_FILE_PATTERN = re.compile(r'^(.+)_(\d+)_(\d+)\.pdf$', re.IGNORECASE)

# File names containing any of these (case-sensitive) text always use page 1.
#   SCHOSC8113550101418817_1_7.pdf  -> page 1 (not 7)
#   10152026123812_S_12923.pdf      -> page 1
PAGE_ONE_MARKERS = ('SCHO', '_S_')
PAGE_ONE = 1

# Mirrors the FileBunker rule:
#   fileNameArray.Length > 1 && fileNameArray[0].ToLower().Contains("booklet")
# Booklet files always use page 1 (e.g. Booklet_1245435.pdf -> page 1).
BOOKLET_MARKER = 'booklet'

# BarcodeScanBy values that mean "not scanned by Inlite"
NOT_SCANNED_VALUES = {'', 'none', 'null', 'nan'}

# Page values that mean "no real page number" (these are never counted)
NA_PAGE_VALUES = {'', 'n/a', 'na', 'none', 'null', 'nan'}

# Columns kept from the Azure Table side before merging
PYTHON_MERGE_COLS = [
    'BlobBackupFilePath', 'OriginalFileName', 'PythonCount', 'PythonCreatedAt', 'PythonPairs', 'DateKey'
]


# Page configuration
st.set_page_config(page_title="Barcode Comparison", layout="wide")


# Sidebar Configuration
with st.sidebar:
    st.header("Configuration")
    st.subheader("Inlite API Configuration")
    st.caption(f"API Base URL: {API_BASE_URL or 'Not configured'}")
    st.caption("Bearer token: Loaded from .env" if API_BEARER_TOKEN else "Bearer token: Missing from .env")

    col1, col2, col3 = st.columns(3)
    with col1:
        url_tenant_id = st.number_input("Tenant ID", value=1, step=1)
    with col2:
        url_enterprise_id = st.number_input("Enterprise ID", value=1, step=1)
    with col3:
        url_store_id = st.number_input("Store ID", value=1, step=1)

    st.subheader("Azure Table Storage")
    st.caption("Connection string: Loaded from .env" if AZURE_STORAGE_CONNECTION_STRING else "Connection string: Missing from .env")
    st.caption("SAS token: Loaded from .env" if AZURE_STORAGE_SAS_TOKEN else "SAS token: Missing from .env (needed for PDF viewing)")
    st.info(f"Table: **{AZURE_TABLE_NAME}**")
    st.divider()
    if API_BASE_URL and API_BEARER_TOKEN and AZURE_STORAGE_CONNECTION_STRING:
        if AZURE_STORAGE_SAS_TOKEN:
            st.success("✅ Configuration loaded. Ready to compare and view PDFs!")
        else:
            st.warning("⚠️ Configuration loaded, but SAS token missing. PDF viewing won't work.")
    else:
        st.warning("Set API_BASE_URL, API_BEARER_TOKEN, AZURE_STORAGE_CONNECTION_STRING, and AZURE_STORAGE_SAS_TOKEN in your .env file.")


# Session state (keeps search results across reruns and pages)
def init_session_state():
    defaults = {
        'searched': False,
        'comparison_df': pd.DataFrame(),
        'inlite_records_by_path': {},
        'inlite_total': 0,
        'python_total': 0,
        'python_skipped': 0,
        'unique_files': 0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# Helper functions

def is_empty_value(value):
    """True for None, NaN, or a blank string."""
    if value is None:
        return True
    if isinstance(value, (list, dict)):
        return False
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return False
    return str(value).strip() == ''


def is_barcode_scanned_by_inlite(value):
    """True only when Inlite's BarcodeScanBy has a real value.

    None, blank, NaN, 'none', and 'null' mean the barcode was not scanned by Inlite.
    """
    if is_empty_value(value):
        return False
    return str(value).strip().lower() not in NOT_SCANNED_VALUES


def is_valid_page(value):
    """True only when a page number is a real value (not None, blank, or N/A)."""
    if is_empty_value(value):
        return False
    return str(value).strip().lower() not in NA_PAGE_VALUES


def normalize_page(value):
    """Return the page as an int, or None if it is missing, N/A, or not a number."""
    if not is_valid_page(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def normalize_rono(value):
    """Return the repair order as a clean string, or None if it is missing or N/A."""
    if is_empty_value(value):
        return None
    text = str(value).strip()
    if text.lower() in NA_PAGE_VALUES:
        return None
    if text.endswith('.0'):
        text = text[:-2]
    return text


def make_barcode_pair(page_value, rono_value):
    """Return (page, repair order), or None when either part is missing."""
    page = normalize_page(page_value)
    rono = normalize_rono(rono_value)
    if page is None or rono is None:
        return None
    return page, rono


def find_column(df, candidates):
    """Find a column ignoring case and underscores."""
    lookup = {c.replace('_', '').lower(): c for c in df.columns}
    for cand in candidates:
        if cand in lookup:
            return lookup[cand]
    return None


def get_field(rec, candidates):
    """Get a value from a record dict, matching keys ignoring case and underscores."""
    lookup = {str(k).replace('_', '').lower(): k for k in rec.keys()}
    for cand in candidates:
        if cand in lookup:
            value = rec[lookup[cand]]
            return None if is_empty_value(value) else value
    return None


def basename_of(name):
    """Return the file name without any folder path."""
    return str(name).replace('\\', '/').split('/')[-1].strip()


def is_booklet_file_name(name):
    """True when the file name splits into more than one '_' part and the first part contains 'booklet'.

    Mirrors the FileBunker rule:
        fileNameArray.Length > 1 && fileNameArray[0].ToLower().Contains("booklet")
    """
    if is_empty_value(name):
        return False
    parts = basename_of(name).split('_')
    return len(parts) > 1 and BOOKLET_MARKER in parts[0].lower()


def is_page_one_file(name):
    """True when the FILE NAME (not its folder) contains SCHO or _S_ (uppercase),
    or is a Booklet file.

    Such files always have their barcode on page 1. Folder names are ignored,
    so a folder such as SchompAutoGroup does not count.
    """
    if is_empty_value(name):
        return False
    file_name = basename_of(name)
    if any(marker in file_name for marker in PAGE_ONE_MARKERS):
        return True
    return is_booklet_file_name(file_name)


def is_booklet_record(rec):
    """True when the record's original or source file name is a Booklet file.

    Checks OriginalFileName and sourceFilePath, since the blob path is a
    generated name that does not show the original file name.
    """
    for candidate in (['originalfilename'], ['sourcefilepath']):
        if is_booklet_file_name(get_field(rec, candidate)):
            return True
    return False


def is_countable_inlite_record(rec):
    """True when an Inlite record should be counted as a barcode.

    Counted when BarcodeScanBy has a value, or when the file is a Booklet file.
    """
    return is_barcode_scanned_by_inlite(get_field(rec, ['barcodescanby'])) or is_booklet_record(rec)


def get_first_original_filename(value):
    """Return the first file name from the Azure Table OriginalFileName list.

    Handles a JSON list string, a real list, a dict with fileName, or a
    comma/semicolon separated string.
    """
    if is_empty_value(value):
        return None

    items = value
    if isinstance(value, str):
        text = value.strip()
        try:
            items = json.loads(text)
        except Exception:
            items = [p for p in re.split(r'[,;\n]+', text) if p.strip()]

    if not isinstance(items, list):
        items = [items]

    for item in items:
        if isinstance(item, dict):
            item = item.get('fileName') or item.get('FileName') or item.get('name')
        if not is_empty_value(item):
            return basename_of(item)
    return None


def parse_split_filename(name):
    """Return (startpage, endpage) from a name like abc_1_7.pdf, or None.

    The barcode was found on endpage.
    """
    if is_empty_value(name):
        return None
    match = SPLIT_FILE_PATTERN.match(basename_of(name))
    if not match:
        return None
    start, end = int(match.group(2)), int(match.group(3))
    if start < 1 or end < start:
        return None
    return start, end


def find_endpage_in_record(rec):
    """Return the endpage from the first split file name in a record's OriginalFileName, or None."""
    value = get_field(rec, ['originalfilename'])
    if value is None:
        return None

    items = value
    if isinstance(value, str):
        try:
            items = json.loads(value.strip())
        except Exception:
            items = [p for p in re.split(r'[,;\n]+', value) if p.strip()]
    if not isinstance(items, list):
        items = [items]

    for item in items:
        span = parse_split_filename(item)
        if span:
            return span[1]
    return None


def get_record_page(rec):
    """Return the Inlite record's page number, or None if it is missing or N/A.

    Files with SCHO or _S_ in their file name, and all Booklet files, are always
    page 1. All other files use the endpage of the split file name, since Inlite
    has no page field. An explicit N/A is never replaced, it is excluded.
    """
    file_path = get_field(rec, ['blobbackupfilepath'])
    original = get_field(rec, ['originalfilename'])
    source = get_field(rec, ['sourcefilepath'])
    if is_page_one_file(file_path) or is_page_one_file(original) or is_page_one_file(source):
        return PAGE_ONE

    page = get_field(rec, ['pagenumber', 'page', 'pageno', 'pagenum'])
    if page is None:
        page = find_endpage_in_record(rec)
    return page if is_valid_page(page) else None


def get_temporary_pdf_uri(blob_path):
    """Build a blob URI with the SAS token from .env

    Takes the blob path and appends the pre-generated SAS token.
    Also adds response-content-disposition=inline to open in browser instead of downloading.
    Returns the full URI or None if token is missing.
    """
    if not AZURE_STORAGE_SAS_TOKEN:
        return None

    blob_path_str = str(blob_path).strip()
    # Remove leading/trailing slashes
    blob_path_str = blob_path_str.strip('/')

    # Build the full blob URI
    blob_uri = f"https://alynkprodstorage.blob.core.windows.net/{blob_path_str}"

    # Append the SAS token (handle both with and without leading ?)
    sas_token = AZURE_STORAGE_SAS_TOKEN.strip()
    if not sas_token.startswith('?'):
        sas_token = f"?{sas_token}"

    # Add response-content-disposition=inline to open in browser instead of downloading
    return f"{blob_uri}{sas_token}&response-content-disposition=inline"


def _has_content(item):
    """True when one entry of a pagenumber list holds something (not null, '', {} or [])."""
    if isinstance(item, (dict, list)):
        return len(item) > 0
    return not is_empty_value(item)


def is_empty_pagenumber(value):
    """True when the Azure Table pagenumber has no entries.

    Treated as empty: [], [ ], [null], [""], [{}], blank, null, None, NaN.
    A list with at least one real entry (even one with N/A pages) is not empty.
    """
    if is_empty_value(value):
        return True

    parsed = value
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in ('[]', 'null', 'none', 'nan', ''):
            return True
        try:
            parsed = json.loads(text)
        except Exception:
            # Not JSON but has text, so it has content
            return False

    if isinstance(parsed, list):
        return not any(_has_content(item) for item in parsed)
    return is_empty_value(parsed)


def parse_python_barcodes(pagenumber):
    """Return the Azure Table pagenumber list as (page, repair order) pairs.

    Azure stores it like [{"page": 1, "rono": "21588659"}, ...]. Entries with
    an N/A page or a missing repair order are skipped.
    """
    try:
        data = pagenumber
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, list):
            return []
        pairs = []
        for item in data:
            if not isinstance(item, dict):
                continue
            pair = make_barcode_pair(item.get('page'), item.get('rono'))
            if pair is not None:
                pairs.append(pair)
        return pairs
    except Exception:
        return []


def count_barcode_differences(inline_pairs, python_pairs):
    """Compare two lists of (page, repair order) pairs.

    Returns (missing_in_python, extra_in_python). Duplicates are counted,
    so the same barcode on the same page twice is handled correctly.
    """
    inline_counts = Counter(inline_pairs)
    python_counts = Counter(python_pairs)
    missing = sum((inline_counts - python_counts).values())
    extra = sum((python_counts - inline_counts).values())
    return missing, extra


def fetch_inlite_data(date_from, date_to):
    """Fetch data from Inlite API - date range only (no company code)"""
    try:
        url = f"{API_BASE_URL}/api/t/{int(url_tenant_id)}/e/{int(url_enterprise_id)}/s/{int(url_store_id)}/filebunker/logs/list"

        headers = {
            "Authorization": f"Bearer {API_BEARER_TOKEN}",
            "Content-Type": "application/json"
        }

        payload = {
            "UrlTenantId": int(url_tenant_id),
            "UrlEnterpriseId": int(url_enterprise_id),
            "UrlStoreId": int(url_store_id),
            "StartDate": date_from.isoformat(),
            "EndDate": date_to.isoformat(),
            "IsOnDemand": True
        }

        st.write(f"**Debug - API URL:** `{url}`")
        st.write(f"**Debug - Payload:** {payload}")

        with st.spinner("Calling Inlite API..."):
            response = requests.get(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()

        data = response.json()

        # Handle different response formats
        if isinstance(data, dict):
            if 'result' in data:
                records = data['result']
            elif 'data' in data:
                records = data['data']
            elif 'value' in data:
                records = data['value']
            else:
                records = data
        else:
            records = data

        if isinstance(records, list):
            return pd.DataFrame(records)
        else:
            st.error("Unexpected response format")
            return pd.DataFrame()

    except Exception as e:
        st.error(f"Error fetching Inlite data: {str(e)}")
        return pd.DataFrame()


def fetch_python_data(date_from, date_to):
    """Fetch Azure Table Storage records by date range only - no store filter.

    CreatedAt may be stored as a String property rather than an Azure Table
    Edm.DateTime value. Filtering it in the Table query with datetime literals
    can therefore return no rows even when records exist.
    """
    try:
        if not AZURE_STORAGE_CONNECTION_STRING:
            st.error("Azure configuration missing! Check AZURE_STORAGE_CONNECTION_STRING in .env.")
            return pd.DataFrame()

        table_client = TableClient.from_connection_string(
            AZURE_STORAGE_CONNECTION_STRING,
            AZURE_TABLE_NAME
        )

        # Fetch all records without store filter - we'll filter by date locally
        # Pass empty filter to get all records
        with st.spinner("Querying Azure Table Storage..."):
            entities = list(table_client.query_entities(query_filter=""))

        if not entities:
            st.warning(
                f"No records found in Azure Table Storage. "
                "Check the table name and storage connection string."
            )
            return pd.DataFrame()

        data = pd.DataFrame(entities)
        st.write(f"**Debug - Azure Table Records:** {len(entities)} total records in table")

        # CreatedAt is stored as a string in the supplied table export, so parse
        # it locally instead of applying Azure Edm.DateTime query filters.
        if "CreatedAt" not in data.columns:
            st.error("Azure Table records do not contain a CreatedAt column.")
            st.caption(f"Available columns: {', '.join(data.columns)}")
            return pd.DataFrame()

        created_at = pd.to_datetime(data["CreatedAt"], errors="coerce", utc=True)
        start = pd.Timestamp(date_from).tz_localize("UTC")
        end = (pd.Timestamp(date_to) + pd.Timedelta(days=1)).tz_localize("UTC")

        # Inclusive date range: start of date_from through the end of date_to.
        data = data.loc[created_at.notna() & (created_at >= start) & (created_at < end)].copy()

        if data.empty:
            st.warning(
                f"Records exist in Azure Table Storage, but none have CreatedAt "
                f"between {date_from.isoformat()} and {date_to.isoformat()}."
            )
            return pd.DataFrame()

        return data.reset_index(drop=True)

    except Exception as e:
        st.error(f"Error fetching Python data from Azure Table Storage: {str(e)}")
        return pd.DataFrame()


def compare_results(inlite_df, python_df):
    """Compare barcodes per file as (page, repair order) pairs.

    Inlite: RecordKey is the repair order. Records are counted when BarcodeScanBy
    has a value or the file is a Booklet file. The page is 1 for SCHO, _S_, and
    Booklet files, otherwise the endpage of the split file name.
    Python: pagenumber holds [{"page": n, "rono": "..."}].

    Azure rows with an empty pagenumber are removed first and never appear.
    Every remaining Azure row appears in the result exactly once. A row with no
    matching Inlite file is kept with an Inlite count of 0.
    """
    # Remove Azure rows whose pagenumber is empty (safety check, also done in run_search)
    if not python_df.empty and 'pagenumber' in python_df.columns:
        python_df = python_df[~python_df['pagenumber'].apply(is_empty_pagenumber)].reset_index(drop=True)

    if inlite_df.empty and python_df.empty:
        return pd.DataFrame()

    try:
        # ---------- Normalize Inlite data ----------
        if inlite_df.empty:
            inlite_data = pd.DataFrame(columns=[
                'BlobBackupFilePath', 'InlinePairs', 'InliteCount', 'CreatedAt', 'DateKey'
            ])
        else:
            inlite_data_source = inlite_df.copy()

            # Normalize column names (case-insensitive)
            api_column_map = {
                'blobbackuppath': 'BlobBackupFilePath',
                'blobbackupfilepath': 'BlobBackupFilePath',
                'originalfilename': 'OriginalFileName',
                'createdat': 'CreatedAt',
                'recordkey': 'RecordKey',
                'barcodescanby': 'BarcodeScanBy',
                'pagecount': 'PageCount',
            }

            rename_dict = {}
            for col in inlite_data_source.columns:
                clean_col = col.replace('_', '').lower()
                if clean_col in api_column_map:
                    rename_dict[col] = api_column_map[clean_col]

            inlite_data_source.rename(columns=rename_dict, inplace=True)

            required = ['BlobBackupFilePath', 'CreatedAt']
            missing = [col for col in required if col not in inlite_data_source.columns]
            if missing:
                st.error(f"Inlite API missing columns: {missing}")
                st.write("Available columns:", list(inlite_data_source.columns))
                return pd.DataFrame()

            if 'OriginalFileName' not in inlite_data_source.columns:
                inlite_data_source['OriginalFileName'] = None
            if 'RecordKey' not in inlite_data_source.columns:
                inlite_data_source['RecordKey'] = None
            if 'BarcodeScanBy' not in inlite_data_source.columns:
                inlite_data_source['BarcodeScanBy'] = None

            if 'PageCount' not in inlite_data_source.columns:
                inlite_data_source['PageCount'] = 0

            inlite_data_source['CreatedAt'] = pd.to_datetime(
                inlite_data_source['CreatedAt'], errors='coerce', utc=True
            )

            # Count rows with BarcodeScanBy filled in, plus all Booklet rows
            scanned_mask = inlite_data_source.apply(
                lambda row: is_countable_inlite_record(row.to_dict()), axis=1
            )
            scanned_rows = inlite_data_source[scanned_mask].copy()

            # One (page, repair order) pair per counted record. N/A pages are skipped.
            pairs_by_file = {}
            for rec in scanned_rows.to_dict('records'):
                pair = make_barcode_pair(get_record_page(rec), get_field(rec, ['recordkey']))
                if pair is None:
                    continue
                file_key = str(rec.get('BlobBackupFilePath', '')).strip()
                pairs_by_file.setdefault(file_key, []).append(pair)

            # Prepare aggregation dict
            agg_dict = {'CreatedAt': 'min'}
            if 'PageCount' in inlite_data_source.columns:
                agg_dict['PageCount'] = 'first'  # PageCount is same for all records of same file

            inlite_data = (
                inlite_data_source
                .groupby(['BlobBackupFilePath'], as_index=False)
                .agg(agg_dict)
            )
            inlite_data['InlinePairs'] = inlite_data['BlobBackupFilePath'].apply(
                lambda p: list(pairs_by_file.get(str(p).strip(), []))
            )
            inlite_data['InliteCount'] = inlite_data['InlinePairs'].apply(len)
            inlite_data['DateKey'] = inlite_data['CreatedAt'].dt.date

            # Ensure PageCount column exists
            if 'PageCount' not in inlite_data.columns:
                inlite_data['PageCount'] = 0

        # ---------- Normalize Azure Table data ----------
        if python_df.empty:
            python_data = pd.DataFrame(columns=PYTHON_MERGE_COLS)
        else:
            python_data = python_df.copy()

            required = ['BlobBackupFilePath', 'CreatedAt', 'pagenumber']
            missing = [col for col in required if col not in python_data.columns]
            if missing:
                st.error(f"Azure Table missing columns: {missing}")
                st.write("Available columns:", list(python_data.columns))
                return pd.DataFrame()

            # First file name from the Azure Table OriginalFileName list
            orig_col = find_column(python_data, ['originalfilename'])
            python_data['OriginalFileName'] = (
                python_data[orig_col].apply(get_first_original_filename) if orig_col else None
            )

            python_data['PythonPairs'] = python_data['pagenumber'].apply(parse_python_barcodes)
            python_data['PythonCount'] = python_data['PythonPairs'].apply(len)
            python_data['PythonCreatedAt'] = pd.to_datetime(python_data['CreatedAt'], errors='coerce', utc=True)
            python_data['DateKey'] = python_data['PythonCreatedAt'].dt.date

            python_data = python_data[PYTHON_MERGE_COLS].copy()

        # Row id so every Azure row can be tracked through the matching steps
        python_data = python_data.reset_index(drop=True)
        python_data['_py_row'] = python_data.index

        # ---------- Match every Azure row to an Inlite file ----------
        # Step 1: same file and same day
        exact = pd.merge(
            python_data, inlite_data,
            on=['BlobBackupFilePath', 'DateKey'], how='inner'
        )
        exact_rows = set(exact['_py_row'])

        # Step 2: same file, any day (only for rows step 1 did not match)
        rest = python_data[~python_data['_py_row'].isin(exact_rows)]
        fallback = pd.merge(
            rest.drop(columns=['DateKey']),
            inlite_data.drop(columns=['DateKey']),
            on=['BlobBackupFilePath'], how='inner'
        )
        fallback_rows = set(fallback['_py_row'])

        # Step 3: no Inlite file at all. Kept with Inlite count 0 so it is not dropped.
        unmatched = rest[~rest['_py_row'].isin(fallback_rows)].copy()
        unmatched['InlinePairs'] = [[] for _ in range(len(unmatched))]
        unmatched['InliteCount'] = 0
        unmatched['PageCount'] = 0
        unmatched['CreatedAt'] = unmatched['PythonCreatedAt']

        comparison = pd.concat([exact, fallback, unmatched], ignore_index=True)

        if len(comparison) == 0:
            st.error("No Azure records to compare in this date range.")
            return pd.DataFrame()

        # ---------- Compare barcodes ----------
        comparison['Difference'] = comparison['InliteCount'] - comparison['PythonCount']

        diffs = [
            count_barcode_differences(inline_pairs, python_pairs)
            for inline_pairs, python_pairs in zip(comparison['InlinePairs'], comparison['PythonPairs'])
        ]
        comparison['MissingInPython'] = [missing for missing, _ in diffs]
        comparison['ExtraInPython'] = [extra for _, extra in diffs]
        comparison['Match'] = [
            'MATCH' if missing == 0 and extra == 0 else 'MISMATCH'
            for missing, extra in diffs
        ]

        final_cols = [
            'BlobBackupFilePath', 'OriginalFileName', 'InliteCount', 'PythonCount', 'PageCount',
            'Difference', 'MissingInPython', 'ExtraInPython', 'Match',
            'CreatedAt', 'InlinePairs', 'PythonPairs'
        ]

        # Fill missing PageCount values with 0
        if 'PageCount' in comparison.columns:
            comparison['PageCount'] = comparison['PageCount'].fillna(0).astype(int)
        else:
            comparison['PageCount'] = 0

        return (
            comparison[final_cols]
            .sort_values('CreatedAt', ascending=False, na_position='last')
            .reset_index(drop=True)
        )

    except Exception as e:
        st.error(f"Error comparing results: {str(e)}")
        return pd.DataFrame()


def run_search(date_from, date_to):
    """Fetch both sources, drop Azure rows with an empty pagenumber, build the comparison, and store it."""
    inlite_df = fetch_inlite_data(date_from, date_to)
    python_df = fetch_python_data(date_from, date_to)

    python_total = len(python_df)

    # Remove Azure rows whose pagenumber is empty ([], blank, null)
    if not python_df.empty and 'pagenumber' in python_df.columns:
        python_df = python_df[~python_df['pagenumber'].apply(is_empty_pagenumber)].reset_index(drop=True)
    python_skipped = python_total - len(python_df)

    # Debug info
    st.write(f"**Debug - Raw Record Counts:**")
    st.write(f"- Inlite API returned: {len(inlite_df)} records")
    st.write(f"- Azure Table in date range: {python_total} records")
    st.write(f"- Azure rows skipped (empty pagenumber): {python_skipped}")
    st.write(f"- Azure rows compared: {len(python_df)}")

    # Index raw Inlite records by file path for the details view
    records_by_path = {}
    unique_paths = set()
    if not inlite_df.empty:
        path_col = find_column(inlite_df, ['blobbackupfilepath'])
        if path_col:
            for _, rec in inlite_df.iterrows():
                key = str(rec[path_col]).strip().lower()
                records_by_path.setdefault(key, []).append(rec.to_dict())
            unique_paths.update(str(p).strip() for p in inlite_df[path_col].dropna())
    if not python_df.empty and 'BlobBackupFilePath' in python_df.columns:
        unique_paths.update(str(p).strip() for p in python_df['BlobBackupFilePath'].dropna())

    comparison = pd.DataFrame()
    if not inlite_df.empty or not python_df.empty:
        comparison = compare_results(inlite_df, python_df)

    st.session_state.comparison_df = comparison
    st.session_state.inlite_records_by_path = records_by_path
    st.session_state.inlite_total = len(inlite_df)
    st.session_state.python_total = python_total
    st.session_state.python_skipped = python_skipped
    st.session_state.unique_files = len(unique_paths)
    st.session_state.searched = True


def render_sidebar_summary():
    """Show record counts from the last search in the sidebar."""
    with st.sidebar:
        st.divider()
        st.subheader("Search Summary")
        if not st.session_state.searched:
            st.caption("Run a search to see record counts.")
            return
        st.metric("Inlite Records", st.session_state.inlite_total)
        st.metric("Python Records (in range)", st.session_state.python_total)
        st.metric("Python Skipped (empty pagenumber)", st.session_state.python_skipped)
        st.metric("Total Unique Files", st.session_state.unique_files)


def render_file_details(row, records_by_path):
    """Detailed view for one selected file: barcode-by-barcode comparison."""
    blob_path = row['BlobBackupFilePath']
    all_recs = records_by_path.get(str(blob_path).strip().lower(), [])

    # Inlite records that are counted as barcodes
    counted_recs = [rec for rec in all_recs if is_countable_inlite_record(rec)]
    inlite_na_count = sum(1 for rec in counted_recs if get_record_page(rec) is None)

    # First file name from the Azure Table OriginalFileName list
    original_filename = None if is_empty_value(row.get('OriginalFileName')) else row['OriginalFileName']

    inline_pairs = row['InlinePairs'] if isinstance(row['InlinePairs'], list) else []
    python_pairs = row['PythonPairs'] if isinstance(row['PythonPairs'], list) else []

    inline_counts = Counter(inline_pairs)
    python_counts = Counter(python_pairs)

    # One line per (page, repair order) found on either side
    detail_rows = []
    for page, rono in sorted(set(inline_counts) | set(python_counts)):
        inline_n = inline_counts.get((page, rono), 0)
        python_n = python_counts.get((page, rono), 0)
        if inline_n == python_n:
            status = '✅ Match'
        elif inline_n > python_n:
            status = '❌ Missing in Python'
        else:
            status = '❌ Extra in Python'
        detail_rows.append({
            'Page #': page,
            'Repair Order': rono,
            'Inlite Count': inline_n,
            'Python Count': python_n,
            'Status': status,
        })

    with st.container(border=True):
        st.markdown(f"**Blob Path:** `{blob_path}`")

        if original_filename:
            st.markdown(f"**Original Filename:** `{original_filename}`")

        if inline_pairs == []:
            st.warning("No Inlite record found for this file in the selected date range.")

        m1, m2, m3, m4, m5 = st.columns(5)
        with m1:
            st.metric("Inlite Barcodes", len(inline_pairs))
        with m2:
            st.metric("Python Barcodes", len(python_pairs))
        with m3:
            st.metric("Missing in Python", int(row['MissingInPython']))
        with m4:
            st.metric("Extra in Python", int(row['ExtraInPython']))
        with m5:
            st.metric("Inlite N/A (excluded)", inlite_na_count)

        st.write(f"**Status:** {row['Match']}")
        st.divider()

        st.subheader("Barcode Comparison (Page + Repair Order)")
        if detail_rows:
            st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)
        else:
            st.info("No barcode data to compare")

        st.divider()
        st.metric("Created", str(row['CreatedAt']).split()[0])


def comparison_page():
    st.title("Barcode Scanner Comparison - Inlite vs Python")

    # Search & Filter
    st.subheader("Search & Filter")

    col1, col2 = st.columns(2)
    with col1:
        date_from = st.date_input(
            "Start Date",
            value=datetime.now().date(),
            help="Select the start date for your search"
        )
    with col2:
        date_to = st.date_input(
            "End Date",
            value=datetime.now().date(),
            help="Select the end date for your search"
        )

    st.divider()

    if date_from > date_to:
        st.error("❌ Start Date cannot be after End Date!")
        search_button = st.button("Search & Compare", use_container_width=True, disabled=True)
    else:
        date_diff = (date_to - date_from).days
        # st.success(f"✅ Ready to search ({date_diff + 1} day{'s' if date_diff != 0 else ''})")
        search_button = st.button("Search & Compare", use_container_width=True)

    if search_button:
        if not API_BASE_URL or not API_BEARER_TOKEN or not AZURE_STORAGE_CONNECTION_STRING:
            st.error("Please configure API and Azure settings in sidebar")
        else:
            with st.spinner("Fetching data from both systems..."):
                run_search(date_from, date_to)

    st.divider()

    if not st.session_state.searched:
        st.info("Click Search & Compare to fetch data.")
        return

    comparison_df = st.session_state.comparison_df
    if comparison_df.empty:
        st.info("No data found. Try checking your configuration.")
        return

    # Results table with search and a View option
    st.subheader(f"Comparison Results ({len(comparison_df)} records)")
    st.caption(
        f"Azure rows in date range: {st.session_state.python_total}  |  "
        f"Skipped (empty pagenumber): {st.session_state.python_skipped}  |  "
        f"Compared: {len(comparison_df)}"
    )

    search_text = st.text_input(
        "Search by file path",
        placeholder="Type part of a file path, e.g. SchompAutoGroup or Booklet_6925370",
        key="file_path_search",
    )

    view_df = comparison_df.reset_index(drop=True)
    if search_text.strip():
        path_mask = view_df['BlobBackupFilePath'].astype(str).str.contains(
            search_text.strip(), case=False, regex=False, na=False
        )
        view_df = view_df[path_mask].reset_index(drop=True)

    if view_df.empty:
        st.info(f"No files match \"{search_text.strip()}\".")
        return

    st.caption(
        f"Showing {len(view_df)} of {len(comparison_df)} files. "
        "Tick **View** on a row to show its barcode details below."
    )

    table_cols = [
        'BlobBackupFilePath', 'OriginalFileName', 'PageCount', 'InliteCount', 'PythonCount',
        'Difference', 'Match', 'CreatedAt'
    ]
    table_df = view_df[table_cols].copy()
    table_df['CreatedAt'] = (
        pd.to_datetime(table_df['CreatedAt'], errors='coerce', utc=True)
        .dt.strftime('%Y-%m-%d %H:%M:%S')
        .fillna('N/A')
    )

    # Add View PDF column with links
    table_df['View PDF'] = table_df['BlobBackupFilePath'].apply(
        lambda path: get_temporary_pdf_uri(path)
    )

    table_df.insert(0, 'View', False)

    # Key includes the search text so View ticks reset when the filter changes
    edited = st.data_editor(
        table_df,
        key=f"results_editor_{search_text.strip().lower()}",
        hide_index=True,
        use_container_width=True,
        disabled=[c for c in table_df.columns if c not in ['View', 'View PDF']],
        column_config={
            'View': st.column_config.CheckboxColumn('View', help='Show details for this file', default=False),
            'View PDF': st.column_config.LinkColumn('View PDF', display_text='📄 Open'),
            'BlobBackupFilePath': st.column_config.TextColumn('File Path', width='large'),
            'OriginalFileName': st.column_config.TextColumn('Original File Name', width='medium'),
            'PageCount': st.column_config.NumberColumn('Total Pages', format='%d'),
            'InliteCount': st.column_config.NumberColumn('Inlite Barcode Count', format='%d'),
            'PythonCount': st.column_config.NumberColumn('Python Barcode Count', format='%d'),
            'Difference': st.column_config.NumberColumn('Difference', format='%d'),
            'Match': st.column_config.TextColumn('Match', width='small'),
            'CreatedAt': st.column_config.TextColumn('Created At', width='medium'),
        }
    )

    # Details for every ticked row
    selected_idx = edited.index[edited['View'].fillna(False).astype(bool)].tolist()
    if selected_idx:
        st.divider()
        st.subheader("File Details")
        for idx in selected_idx:
            render_file_details(view_df.loc[idx], st.session_state.inlite_records_by_path)

    st.divider()

    # Export (exports the files currently shown, excluding the PDF link column)
    export_df = view_df[table_cols]  # Use original table_cols without View PDF
    st.download_button(
        label="Download Comparison (CSV)",
        data=export_df.to_csv(index=False),
        file_name=f"barcode_comparison_{date_from}_{date_to}.csv",
        mime="text/csv",
        use_container_width=True
    )

    with st.expander("Help & Troubleshooting"):
        st.markdown("""
        ### How Barcodes Are Matched:

        Each barcode is identified by its **page number** and **repair order**.

        - **Inlite**: the repair order is the **RecordKey**. A record is counted when
          **BarcodeScanBy** is filled in, or when its file name's first part (before the
          first `_`) contains **Booklet**, for example `Booklet_1245435.pdf`, even if
          BarcodeScanBy is empty.
          The barcode page is **1** for these files:
          - Files with **SCHO** in the file name (for example `SCHOSC8113550101418817_1_7.pdf`)
          - Files with **_S_** in the file name (for example `10152026123812_S_12923.pdf`)
          - Booklet files (for example `Booklet_1245435.pdf`)

          For all other files, the page is the endpage of the split file name
          (for example `10102026115317_177_177.pdf` is page 177). Records with an
          **N/A** page are not counted.
        - **Python**: the **pagenumber** list gives each page and its repair order
          (`[{"page": 1, "rono": "21588659"}, ...]`). Entries with an N/A page are not counted.

        A file is **MATCH** only when both sides have the same page and repair order pairs.
        The file details view shows the missing and extra barcodes for each file.

        ### Which Files Are Compared:

        Azure Table records with an empty **pagenumber** (`[]`, blank, or null) are removed
        before comparison and never appear in the results. The caption above the table shows
        how many were skipped.

        If an Azure file has no matching Inlite file in the date range, it still appears in the
        results with an Inlite Barcode Count of 0 and status **MISMATCH**.

        ### File Name:

        **Original File Name** is the first entry of the **OriginalFileName** list stored in
        the Azure Table.

        ### Searching:

        Type part of a file path in **Search by file path** to filter the results table.
        The search is not case-sensitive. Clear the box to show all files again.

        ### Column Meanings:

        - **Inlite Barcode Count**: Counted Inlite barcodes for the file with a real page number
        - **Python Barcode Count**: Barcodes in the Python pagenumber list with a real page number
        - **Difference**: Inlite Count - Python Count
        - **Match**: MATCH if every barcode pair is on both sides, MISMATCH otherwise

        ### Troubleshooting:

        - **Count is wrong**: Check BarcodeScanBy, RecordKey, and the split file name on the raw Inlite records
        - **No data found**: Check dates and that both systems have data for the selected days
        - **API Error**: Check base URL, token, and IDs
        - **PDF link not working**: Ensure the blob path is accessible from your network
        """)


def statistics_page():
    st.title("Statistics & Analysis")

    if not st.session_state.searched:
        st.info("Run a search on the Comparison page first.")
        return

    comparison_df = st.session_state.comparison_df
    if comparison_df.empty:
        st.info("No comparison data. Run a search on the Comparison page.")
        return

    total = len(comparison_df)
    matches = int((comparison_df['Match'] == 'MATCH').sum())
    mismatches = total - matches
    accuracy = (matches / total * 100) if total else 0
    avg_difference = comparison_df['Difference'].abs().mean()

    total_inlite = int(comparison_df['InliteCount'].sum())
    total_python = int(comparison_df['PythonCount'].sum())
    total_missing = int(comparison_df['MissingInPython'].sum())
    total_extra = int(comparison_df['ExtraInPython'].sum())
    inlite_higher = int((comparison_df['Difference'] > 0).sum())
    python_higher = int((comparison_df['Difference'] < 0).sum())

    st.subheader("Overview")
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Files Compared", total)
    with c2:
        st.metric("Matches", matches)
    with c3:
        st.metric("Mismatches", mismatches)
    with c4:
        st.metric("Accuracy", f"{accuracy:.1f}%")
    with c5:
        st.metric("Avg Difference", f"{avg_difference:.1f}")

    st.subheader("Barcode Totals")
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        st.metric("Total Inlite Barcodes", total_inlite)
    with t2:
        st.metric("Total Python Barcodes", total_python)
    with t3:
        st.metric("Inlite Higher", inlite_higher)
    with t4:
        st.metric("Python Higher", python_higher)

    st.subheader("Barcode Differences (Page + Repair Order)")
    d1, d2 = st.columns(2)
    with d1:
        st.metric("Missing in Python", total_missing)
    with d2:
        st.metric("Extra in Python", total_extra)

    st.subheader("Match Status")
    st.bar_chart(pd.DataFrame({'Files': [matches, mismatches]}, index=['Match', 'Mismatch']))

    st.divider()

    left, right = st.columns(2)
    with left:
        st.subheader("Matching Records")
        matches_df = comparison_df[comparison_df['Match'] == 'MATCH']
        if not matches_df.empty:
            st.dataframe(
                matches_df[['BlobBackupFilePath', 'OriginalFileName', 'InliteCount', 'PythonCount']],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("No matching records")

    with right:
        st.subheader("Mismatched Records")
        mismatches_df = comparison_df[comparison_df['Match'] == 'MISMATCH']
        if not mismatches_df.empty:
            st.dataframe(
                mismatches_df[['BlobBackupFilePath', 'OriginalFileName', 'InliteCount', 'PythonCount', 'Difference']],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("No mismatched records")


# Navigation between pages
init_session_state()

pages = [
    st.Page(comparison_page, title="Comparison", default=True),
    st.Page(statistics_page, title="Statistics & Analysis"),
]
navigation = st.navigation(pages)
navigation.run()

render_sidebar_summary()