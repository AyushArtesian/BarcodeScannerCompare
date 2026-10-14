import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta
from azure.data.tables import TableClient
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


def build_inlite_barcode_rows(records):
    """Convert scanned Inlite records into the same shape as the Python barcode details.

    Page # comes from the endpage in the split filename, if the record has one.
    Repair Order comes from RecordKey.
    """
    rows = []
    for rec in records:
        page = get_field(rec, ['pagenumber', 'page', 'pageno', 'pagenum'])
        if page is None:
            page = find_endpage_in_record(rec)
        rono = get_field(rec, ['recordkey'])
        rows.append({
            'Page #': page if page is not None else 'N/A',
            'Repair Order': str(rono) if rono is not None else 'N/A'
        })
    return rows


def fetch_inlite_data(company_code, date_from, date_to):
    """Fetch data from Inlite API"""
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
            "CompanyCode": company_code
        }

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


def fetch_python_data(company_code, date_from, date_to):
    """Fetch Azure Table Storage records by Store, then filter CreatedAt locally.

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

        # Escape apostrophes for an OData string literal.
        escaped_company_code = str(company_code).replace("'", "''")
        filter_query = f"Store eq '{escaped_company_code}'"

        with st.spinner("Querying Azure Table Storage..."):
            entities = list(table_client.query_entities(query_filter=filter_query))

        if not entities:
            st.warning(
                f"No records found in Azure Table Storage for Store '{company_code}'. "
                "Check the table name, storage connection string, and Store value."
            )
            return pd.DataFrame()

        data = pd.DataFrame(entities)

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
                f"Records exist for Store '{company_code}', but none have CreatedAt "
                f"between {date_from.isoformat()} and {date_to.isoformat()}."
            )
            return pd.DataFrame()

        return data.reset_index(drop=True)

    except Exception as e:
        st.error(f"Error fetching Python data from Azure Table Storage: {str(e)}")
        return pd.DataFrame()


def get_barcode_count_from_python(pagenumber):
    """Extract barcode count from pagenumber array"""
    try:
        if isinstance(pagenumber, str):
            pagenumber = json.loads(pagenumber)
        if isinstance(pagenumber, list):
            return len(pagenumber)
        return 0
    except Exception:
        return 0


def get_file_count_from_python(original_filename):
    """Extract file count from OriginalFileName array"""
    try:
        if isinstance(original_filename, str):
            original_filename = json.loads(original_filename)
        if isinstance(original_filename, list):
            return len(original_filename)
        return 0
    except Exception:
        return 0


def parse_page_details(pagenumber, original_filename):
    """Parse page details for the details view"""
    try:
        # Handle OriginalFileName
        files = []
        if original_filename:
            if isinstance(original_filename, str):
                try:
                    files = json.loads(original_filename)
                except Exception:
                    try:
                        cleaned = original_filename.replace('""', '"')
                        files = json.loads(cleaned)
                    except Exception:
                        files = [original_filename]
            elif isinstance(original_filename, list):
                files = original_filename

        # Handle pagenumber
        pages = []
        if pagenumber:
            if isinstance(pagenumber, str):
                try:
                    pages = json.loads(pagenumber)
                except Exception:
                    try:
                        cleaned = pagenumber.replace('""', '"')
                        pages = json.loads(cleaned)
                    except Exception:
                        pages = []
            elif isinstance(pagenumber, list):
                pages = pagenumber

        # Ensure they are lists
        if not isinstance(files, list):
            files = [files] if files else []
        if not isinstance(pages, list):
            pages = [pages] if pages else []

        return {'files': files, 'pages': pages}
    except Exception:
        return {'files': [], 'pages': []}


def compare_results(inlite_df, python_df):
    """Normalize both data sources and compare their barcode counts."""
    if inlite_df.empty and python_df.empty:
        return pd.DataFrame()

    try:
        # Normalize Inlite data
        if inlite_df.empty:
            inlite_data = pd.DataFrame(columns=[
                'StoreCode', 'BlobBackupFilePath', 'OriginalFileName',
                'InliteCount', 'BarcodeKeys', 'CreatedAt', 'DateKey'
            ])
        else:
            inlite_data_source = inlite_df.copy()

            # Normalize column names (case-insensitive)
            api_column_map = {
                'storecode': 'StoreCode',
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

            required = ['StoreCode', 'BlobBackupFilePath', 'CreatedAt']
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
            scanned_rows = inlite_data_source[scanned_mask]

            scanned_counts = (
                scanned_rows
                .groupby(['StoreCode', 'BlobBackupFilePath'], as_index=False)
                .agg(
                    InliteCount=('RecordKey', 'size'),
                    BarcodeKeys=('RecordKey', lambda s: sorted({str(x) for x in s if not is_empty_value(x)})),
                )
            )

            inlite_base = (
                inlite_data_source
                .groupby(['StoreCode', 'BlobBackupFilePath'], as_index=False)
                .agg(
                    OriginalFileName=('OriginalFileName', 'first'),
                    CreatedAt=('CreatedAt', 'min'),
                )
            )

            inlite_data = inlite_base.merge(
                scanned_counts, on=['StoreCode', 'BlobBackupFilePath'], how='left'
            )
            inlite_data['InliteCount'] = inlite_data['InliteCount'].fillna(0).astype(int)
            inlite_data['BarcodeKeys'] = inlite_data['BarcodeKeys'].apply(
                lambda v: v if isinstance(v, list) else []
            )
            inlite_data['DateKey'] = inlite_data['CreatedAt'].dt.date

        # Normalize Azure Table data
        if python_df.empty:
            python_data = pd.DataFrame(columns=[
                'StoreCode', 'BlobBackupFilePath', 'OriginalFileName',
                'PythonCount', 'FileCount', 'CreatedAt', 'pagenumber', 'DateKey'
            ])
        else:
            python_data = python_df.copy()

            required = ['Store', 'BlobBackupFilePath', 'CreatedAt', 'pagenumber']
            missing = [col for col in required if col not in python_data.columns]
            if missing:
                st.error(f"Azure Table missing columns: {missing}")
                st.write("Available columns:", list(python_data.columns))
                return pd.DataFrame()

            python_data['PythonCount'] = python_data['pagenumber'].apply(get_barcode_count_from_python)
            python_data['FileCount'] = python_data['OriginalFileName'].apply(get_file_count_from_python)
            python_data['CreatedAt'] = pd.to_datetime(python_data['CreatedAt'], errors='coerce', utc=True)
            python_data.rename(columns={'Store': 'StoreCode'}, inplace=True)
            python_data['DateKey'] = python_data['CreatedAt'].dt.date

            python_data = python_data[[
                'StoreCode', 'BlobBackupFilePath', 'OriginalFileName',
                'PythonCount', 'FileCount', 'CreatedAt', 'pagenumber', 'DateKey'
            ]].copy()

        # INNER JOIN - only matching files
        comparison = pd.merge(
            inlite_data,
            python_data[[
                'StoreCode', 'BlobBackupFilePath', 'OriginalFileName',
                'PythonCount', 'FileCount', 'CreatedAt', 'pagenumber', 'DateKey'
            ]],
            on=['StoreCode', 'BlobBackupFilePath', 'DateKey'],
            how='inner',
            suffixes=('_inlite', '_python')
        )

        if len(comparison) == 0:
            comparison = pd.merge(
                inlite_data,
                python_data[[
                    'StoreCode', 'BlobBackupFilePath', 'OriginalFileName',
                    'PythonCount', 'FileCount', 'CreatedAt', 'pagenumber'
                ]],
                on=['StoreCode', 'BlobBackupFilePath'],
                how='inner',
                suffixes=('_inlite', '_python')
            )

        if len(comparison) == 0:
            st.error("No matching records found between Inlite and Azure data!")
            return pd.DataFrame()

        # Handle CreatedAt columns (may be suffixed if they came from both sources)
        if 'CreatedAt_inlite' in comparison.columns:
            comparison['CreatedAt'] = comparison['CreatedAt_inlite']
            comparison.drop(columns=['CreatedAt_inlite'], inplace=True)
        if 'CreatedAt_python' in comparison.columns:
            comparison.drop(columns=['CreatedAt_python'], inplace=True)

        comparison.drop(columns=['DateKey'], errors='ignore', inplace=True)

        if 'CreatedAt' not in comparison.columns:
            st.error("CreatedAt column missing after merge!")
            return pd.DataFrame()

        # Calculate differences
        comparison['Difference'] = comparison['InliteCount'] - comparison['PythonCount']
        comparison['Match'] = comparison.apply(
            lambda row: 'MATCH' if row['InliteCount'] == row['PythonCount'] else 'MISMATCH',
            axis=1
        )

        final_cols = [
            'StoreCode', 'BlobBackupFilePath', 'InliteCount', 'PythonCount',
            'FileCount', 'Difference', 'Match', 'CreatedAt', 'BarcodeKeys'
        ]

        if 'OriginalFileName_inlite' in comparison.columns:
            final_cols.append('OriginalFileName_inlite')
        if 'OriginalFileName_python' in comparison.columns:
            final_cols.append('OriginalFileName_python')
        if 'OriginalFileName' in comparison.columns and 'OriginalFileName_inlite' not in comparison.columns and 'OriginalFileName_python' not in comparison.columns:
            final_cols.append('OriginalFileName')

        if 'pagenumber' in comparison.columns:
            final_cols.append('pagenumber')

        return comparison[final_cols].sort_values('CreatedAt', ascending=False, na_position='last').reset_index(drop=True)

    except Exception as e:
        st.error(f"Error comparing results: {str(e)}")
        return pd.DataFrame()


def run_search(company_code, date_from, date_to):
    """Fetch both sources, build the comparison, and store everything in session state."""
    inlite_df = fetch_inlite_data(company_code, date_from, date_to)
    python_df = fetch_python_data(company_code, date_from, date_to)

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
    """Detailed view for one selected file."""
    blob_path = row['BlobBackupFilePath']
    all_recs = records_by_path.get(str(blob_path).strip().lower(), [])
    scanned_recs = [
        rec for rec in all_recs
        if is_barcode_scanned_by_inlite(get_field(rec, ['barcodescanby']))
    ]
    excluded = len(all_recs) - len(scanned_recs)

    python_filename = row.get('OriginalFileName_python', row.get('OriginalFileName', None))
    python_details = parse_page_details(row.get('pagenumber', None), python_filename)
    python_files = python_details['files']
    python_pages = python_details['pages']

    with st.container(border=True):
        st.markdown(f"**File:** `{blob_path}`")

        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Inlite Count", row['InliteCount'])
        with m2:
            st.metric("Python Count", row['PythonCount'])
        with m3:
            st.metric("Difference", row['Difference'])

        st.write(f"**Status:** {row['Match']}")
        st.divider()

        c1, c2 = st.columns(2)

        with c1:
            st.subheader("Inlite Barcodes")
            if scanned_recs:
                st.dataframe(
                    pd.DataFrame(build_inlite_barcode_rows(scanned_recs)).astype(str),
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.info("No barcodes scanned by Inlite")
            st.caption(f"Total: {row['InliteCount']} barcode(s) scanned by Inlite")
            st.caption(f"Records excluded (BarcodeScanBy empty): {excluded}")

        with c2:
            st.subheader("Python Files")
            if python_files:
                for i, file in enumerate(python_files, 1):
                    st.write(f"{i}. {file}")
            else:
                st.info("No files found in Python")

            st.subheader("Python Barcodes")
            page_data = []
            for page_info in python_pages:
                if isinstance(page_info, dict):
                    page_data.append({
                        'Page #': page_info.get('page', 'N/A'),
                        'Repair Order': page_info.get('rono', 'N/A')
                    })
            if page_data:
                st.dataframe(pd.DataFrame(page_data), use_container_width=True, hide_index=True)
            else:
                st.info("No barcode details found")
            st.caption(f"Total: {row['PythonCount']} barcode(s) detected")

        st.caption(f"Created: {row['CreatedAt']}")


def comparison_page():
    st.title("Barcode Scanner Comparison - Inlite vs Python")

    # Search & Filter
    st.subheader("Search & Filter")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        company_code = st.text_input(
            "Company Code",
            value="SC1",
            placeholder="e.g., SC1",
            help="Company code from your system"
        )
    with col2:
        date_from = st.date_input(
            "Start Date",
            value=datetime.now() - timedelta(days=7),
            help="Start date for filtering"
        )
    with col3:
        date_to = st.date_input(
            "End Date",
            value=datetime.now(),
            help="End date for filtering"
        )
    with col4:
        st.write("")
        st.write("")
        search_button = st.button("Search & Compare", use_container_width=True)

    if search_button:
        if not company_code:
            st.error("Please enter a Company Code")
        elif not API_BASE_URL or not API_BEARER_TOKEN or not AZURE_STORAGE_CONNECTION_STRING:
            st.error("Please configure API and Azure settings in sidebar")
        else:
            with st.spinner("Fetching data from both systems..."):
                run_search(company_code, date_from, date_to)

    st.divider()

    if not st.session_state.searched:
        st.info("Choose your filters and click Search & Compare.")
        return

    comparison_df = st.session_state.comparison_df
    if comparison_df.empty:
        st.info("No data found. Try adjusting filters or checking your configuration.")
        return

    # Results table with a View option
    st.subheader(f"Comparison Results ({len(comparison_df)} records)")
    st.caption("Tick **View** on a row to show its details below.")

    view_df = comparison_df.reset_index(drop=True)
    table_df = view_df[[
        'StoreCode', 'BlobBackupFilePath', 'InliteCount', 'PythonCount',
        'FileCount', 'Difference', 'Match', 'CreatedAt'
    ]].copy()
    table_df['CreatedAt'] = (
        pd.to_datetime(table_df['CreatedAt'], errors='coerce', utc=True)
        .dt.strftime('%Y-%m-%d %H:%M:%S')
        .fillna('N/A')
    )
    table_df.insert(0, 'View', False)

    edited = st.data_editor(
        table_df,
        key='results_editor',
        hide_index=True,
        use_container_width=True,
        disabled=[c for c in table_df.columns if c != 'View'],
        column_config={
            'View': st.column_config.CheckboxColumn('View', help='Show details for this file', default=False),
            'StoreCode': st.column_config.TextColumn('Store', width='small'),
            'BlobBackupFilePath': st.column_config.TextColumn('File Path', width='large'),
            'InliteCount': st.column_config.NumberColumn('Inlite Barcode Count', format='%d'),
            'PythonCount': st.column_config.NumberColumn('Python Barcode Count', format='%d'),
            'FileCount': st.column_config.NumberColumn('Files Scanned', format='%d'),
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

    # Export
    export_df = view_df[[
        'StoreCode', 'BlobBackupFilePath', 'InliteCount',
        'PythonCount', 'FileCount', 'Difference', 'Match', 'CreatedAt'
    ]]
    st.download_button(
        label="Download Comparison (CSV)",
        data=export_df.to_csv(index=False),
        file_name=f"barcode_comparison_{company_code}_{date_from}_{date_to}.csv",
        mime="text/csv",
        use_container_width=True
    )

    with st.expander("Help & Troubleshooting"):
        st.markdown("""
        ### How Inlite Barcode Count Works:

        A record counts as an Inlite barcode only when its **BarcodeScanBy** has a value.
        Records where BarcodeScanBy is empty, None, or null were not scanned by Inlite.
        Their repair order is entered another way, so they are not counted.

        ### Column Meanings:

        - **Inlite Barcode Count**: Number of Inlite records for the file where BarcodeScanBy has a value
        - **Python Barcode Count**: Number of barcodes detected by your Python pipeline
        - **Files Scanned**: Number of files in the Python record's OriginalFileName list (the source PDF and its split files)
        - **Difference**: Inlite Count - Python Count
        - **Match**: MATCH if counts match, MISMATCH if different

        ### Troubleshooting:

        - **Inlite count is wrong**: Check the BarcodeScanBy value on the raw Inlite records for that file
        - **No data found**: Check company code, dates, and that both systems have data
        - **API Error**: Check base URL, token, and IDs
        - **Inlite details empty**: Check that BlobBackupFilePath matches exactly between systems
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

    st.subheader("Match Status")
    st.bar_chart(pd.DataFrame({'Files': [matches, mismatches]}, index=['Match', 'Mismatch']))

    st.divider()

    left, right = st.columns(2)
    with left:
        st.subheader("Matching Records")
        matches_df = comparison_df[comparison_df['Match'] == 'MATCH']
        if not matches_df.empty:
            st.dataframe(
                matches_df[['BlobBackupFilePath', 'InliteCount', 'PythonCount', 'FileCount']],
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