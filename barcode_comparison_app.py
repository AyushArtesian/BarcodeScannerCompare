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
AZURE_TABLE_NAME = os.getenv("AZURE_TABLE_NAME", "BarcodeScanResults")

# Split PDF names look like abc_startpage_endpage.pdf (e.g. Booklet_21615575_1_7.pdf)
SPLIT_FILE_PATTERN = re.compile(r'^(.+)_(\d+)_(\d+)\.pdf$', re.IGNORECASE)

# BarcodeScanBy values that mean "not scanned by Inlite"
NOT_SCANNED_VALUES = {'', 'none', 'null', 'nan'}

# Page values that mean "no real page number" (these are never counted)
NA_PAGE_VALUES = {'', 'n/a', 'na', 'none', 'null', 'nan'}

# Columns kept from the Azure Table side before merging
PYTHON_MERGE_COLS = [
    'BlobBackupFilePath', 'PythonCount', 'PythonCreatedAt', 'PythonPairs', 'DateKey'
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
    st.info(f"Table: **{AZURE_TABLE_NAME}**")
    st.divider()
    if API_BASE_URL and API_BEARER_TOKEN and AZURE_STORAGE_CONNECTION_STRING:
        st.success("Configuration loaded. Ready to compare!")
    else:
        st.warning("Set API_BASE_URL, API_BEARER_TOKEN, and AZURE_STORAGE_CONNECTION_STRING in your .env file.")


# Session state (keeps search results across reruns and pages)
def init_session_state():
    defaults = {
        'searched': False,
        'comparison_df': pd.DataFrame(),
        'inlite_records_by_path': {},
        'inlite_total': 0,
        'python_total': 0,
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

    None, blank, NaN, 'none', and 'null' mean the barcode was not scanned by Inlite,
    so that record is not counted. Its repair order is entered by another method.
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

    Inlite has no page field, so the page comes from the endpage of the split
    file name. An explicit N/A is never replaced, it is excluded.
    """
    page = get_field(rec, ['pagenumber', 'page', 'pageno', 'pagenum'])
    if page is None:
        page = find_endpage_in_record(rec)
    return page if is_valid_page(page) else None


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


def has_python_pagenumber(pagenumber):
    """True when the Azure Table pagenumber is not empty (ignores '[]', blank, and null)."""
    if is_empty_value(pagenumber):
        return False
    parsed = pagenumber
    if isinstance(pagenumber, str):
        try:
            parsed = json.loads(pagenumber.strip())
        except Exception:
            return pagenumber.strip() not in ('', '[]', 'null')
    if isinstance(parsed, list):
        return len(parsed) > 0
    return not is_empty_value(parsed)


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

    Inlite: RecordKey is the repair order, the split file name endpage is the page.
    Python: pagenumber holds [{"page": n, "rono": "..."}].
    """
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
                st.error("Inlite API response has no BarcodeScanBy column, so Inlite barcodes cannot be counted.")
                st.write("Available columns:", list(inlite_data_source.columns))
                return pd.DataFrame()

            inlite_data_source['CreatedAt'] = pd.to_datetime(
                inlite_data_source['CreatedAt'], errors='coerce', utc=True
            )

            # Only rows scanned by Inlite (BarcodeScanBy has a value) are counted.
            scanned_mask = inlite_data_source['BarcodeScanBy'].apply(is_barcode_scanned_by_inlite)
            scanned_rows = inlite_data_source[scanned_mask].copy()

            # One (page, repair order) pair per scanned record. N/A pages are skipped.
            pairs_by_file = {}
            for rec in scanned_rows.to_dict('records'):
                pair = make_barcode_pair(get_record_page(rec), get_field(rec, ['recordkey']))
                if pair is None:
                    continue
                file_key = str(rec.get('BlobBackupFilePath', '')).strip()
                pairs_by_file.setdefault(file_key, []).append(pair)

            inlite_data = (
                inlite_data_source
                .groupby(['BlobBackupFilePath'], as_index=False)
                .agg(CreatedAt=('CreatedAt', 'min'))
            )
            inlite_data['InlinePairs'] = inlite_data['BlobBackupFilePath'].apply(
                lambda p: list(pairs_by_file.get(str(p).strip(), []))
            )
            inlite_data['InliteCount'] = inlite_data['InlinePairs'].apply(len)
            inlite_data['DateKey'] = inlite_data['CreatedAt'].dt.date

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

            python_data['PythonPairs'] = python_data['pagenumber'].apply(parse_python_barcodes)
            python_data['PythonCount'] = python_data['PythonPairs'].apply(len)
            python_data['PythonCreatedAt'] = pd.to_datetime(python_data['CreatedAt'], errors='coerce', utc=True)
            python_data['DateKey'] = python_data['PythonCreatedAt'].dt.date

            python_data = python_data[PYTHON_MERGE_COLS].copy()

        # ---------- Match files: same file and same day first, then file only ----------
        comparison = pd.merge(
            inlite_data,
            python_data,
            on=['BlobBackupFilePath', 'DateKey'],
            how='inner'
        )

        if len(comparison) == 0:
            comparison = pd.merge(
                inlite_data,
                python_data.drop(columns=['DateKey']),
                on=['BlobBackupFilePath'],
                how='inner'
            )

        if len(comparison) == 0:
            st.error("No matching records found between Inlite and Azure data!")
            st.write("**Debug Info:**")
            st.write(f"Inlite records (after normalization): {len(inlite_data)}")
            if len(inlite_data) > 0:
                st.write("Inlite columns:", list(inlite_data.columns))
                st.write("Inlite sample BlobBackupFilePath values:", inlite_data['BlobBackupFilePath'].head(3).tolist())
            st.write(f"Python records (after normalization): {len(python_data)}")
            if len(python_data) > 0:
                st.write("Python columns:", list(python_data.columns))
                st.write("Python sample BlobBackupFilePath values:", python_data['BlobBackupFilePath'].head(3).tolist())
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
            'BlobBackupFilePath', 'InliteCount', 'PythonCount',
            'Difference', 'MissingInPython', 'ExtraInPython', 'Match',
            'CreatedAt', 'InlinePairs', 'PythonPairs'
        ]

        return (
            comparison[final_cols]
            .sort_values('CreatedAt', ascending=False, na_position='last')
            .reset_index(drop=True)
        )

    except Exception as e:
        st.error(f"Error comparing results: {str(e)}")
        return pd.DataFrame()


def run_search(date_from, date_to):
    """Fetch both sources, keep only Azure rows with a page number, build the comparison, and store it."""
    inlite_df = fetch_inlite_data(date_from, date_to)
    python_df = fetch_python_data(date_from, date_to)

    # Compare only files whose Azure Table pagenumber is not empty
    if not python_df.empty and 'pagenumber' in python_df.columns:
        python_df = python_df[python_df['pagenumber'].apply(has_python_pagenumber)].reset_index(drop=True)

    # Debug info
    st.write(f"**Debug - Raw Record Counts:**")
    st.write(f"- Inlite API returned: {len(inlite_df)} records")
    st.write(f"- Azure Table returned (with pagenumber): {len(python_df)} records")

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
    st.session_state.python_total = len(python_df)
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
        st.metric("Python Records", st.session_state.python_total)
        st.metric("Total Unique Files", st.session_state.unique_files)


def render_file_details(row, records_by_path):
    """Detailed view for one selected file: barcode-by-barcode comparison."""
    blob_path = row['BlobBackupFilePath']
    all_recs = records_by_path.get(str(blob_path).strip().lower(), [])

    # Inlite records that were scanned by Inlite
    scanned_recs = [
        rec for rec in all_recs
        if is_barcode_scanned_by_inlite(get_field(rec, ['barcodescanby']))
    ]
    inlite_na_count = sum(1 for rec in scanned_recs if get_record_page(rec) is None)

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
        st.markdown(f"**File:** `{blob_path}`")

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
            value=datetime.now().date() - timedelta(days=2),
            help="Select the start date for your search"
        )
    with col2:
        date_to = st.date_input(
            "End Date",
            value=datetime.now().date(),
            help="Select the end date for your search"
        )

    # Validate date range: must be <= 2 days
    date_diff = (date_to - date_from).days

    st.divider()

    if date_from > date_to:
        st.error("❌ Start Date cannot be after End Date!")
        search_button = st.button("Search & Compare", use_container_width=True, disabled=True)
    elif date_diff > 2:
        st.error(f"❌ Date range is {date_diff} days. Maximum allowed is 2 days.")
        st.caption("Please select dates within 2 days of each other.")
        search_button = st.button("Search & Compare", use_container_width=True, disabled=True)
    else:
        st.success(f"✅ Date range is valid ({date_diff} day{'s' if date_diff != 1 else ''}) - Ready to search")
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
        'BlobBackupFilePath', 'InliteCount', 'PythonCount',
        'Difference', 'Match', 'CreatedAt'
    ]
    table_df = view_df[table_cols].copy()
    table_df['CreatedAt'] = (
        pd.to_datetime(table_df['CreatedAt'], errors='coerce', utc=True)
        .dt.strftime('%Y-%m-%d %H:%M:%S')
        .fillna('N/A')
    )
    table_df.insert(0, 'View', False)

    # Key includes the search text so View ticks reset when the filter changes
    edited = st.data_editor(
        table_df,
        key=f"results_editor_{search_text.strip().lower()}",
        hide_index=True,
        use_container_width=True,
        disabled=[c for c in table_df.columns if c != 'View'],
        column_config={
            'View': st.column_config.CheckboxColumn('View', help='Show details for this file', default=False),
            'BlobBackupFilePath': st.column_config.TextColumn('File Path', width='large'),
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

    # Export (exports the files currently shown)
    export_df = view_df[table_cols]
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

        - **Inlite**: the repair order is the **RecordKey**, and the page is the endpage
          of the split file name (for example `10102026115317_177_177.pdf` is page 177).
          Only records with **BarcodeScanBy** filled in are counted. Records with an
          **N/A** page are not counted.
        - **Python**: the **pagenumber** list gives each page and its repair order
          (`[{"page": 1, "rono": "21588659"}, ...]`). Entries with an N/A page are not counted.

        A file is **MATCH** only when both sides have the same page and repair order pairs.
        The file details view shows the missing and extra barcodes for each file.

        ### Which Files Are Compared:

        Only Azure Table records with a non-empty **pagenumber** are compared. Records
        showing an empty list (`[]`) are excluded from the comparison and the counts.

        ### Searching:

        Type part of a file path in **Search by file path** to filter the results table.
        The search is not case-sensitive. Clear the box to show all files again.

        ### Column Meanings:

        - **Inlite Barcode Count**: Scanned Inlite barcodes for the file with a real page number
        - **Python Barcode Count**: Barcodes in the Python pagenumber list with a real page number
        - **Difference**: Inlite Count - Python Count
        - **Match**: MATCH if every barcode pair is on both sides, MISMATCH otherwise

        ### Troubleshooting:

        - **Count is wrong**: Check BarcodeScanBy, RecordKey, and the split file name on the raw Inlite records
        - **No data found**: Check dates and that both systems have data for the selected days
        - **API Error**: Check base URL, token, and IDs
        - **Details empty**: Check that BlobBackupFilePath matches exactly between systems
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
                matches_df[['BlobBackupFilePath', 'InliteCount', 'PythonCount']],
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
                mismatches_df[['BlobBackupFilePath', 'InliteCount', 'PythonCount', 'Difference']],
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