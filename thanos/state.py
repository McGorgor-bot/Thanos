import hashlib
import pandas as pd
import streamlit as st
from .config import DEFAULT_PATTERNS, RESULT_COLUMNS

def _uploaded_files_fingerprint(files, page='pdf'):
    """Fingerprint the uploaded document set without re-hashing large files on every rerun.

    Streamlit UploadedFile objects expose a server-side ``file_id`` that changes
    when the uploaded file changes. Use that stable identity plus name and size
    for the normal UI path. For custom/test file objects without a file_id, fall
    back to a content digest so same-name/same-size content changes are detected.
    """
    entries = []
    for f in (files or []):
        name = str(getattr(f, 'name', ''))
        size = getattr(f, 'size', None)
        file_id = getattr(f, 'file_id', None)
        if file_id:
            entries.append((name, str(file_id), str(size if size is not None else '')))
            continue
        try:
            data = f.getvalue()
        except Exception:
            data = b''
        digest = hashlib.sha256(data).hexdigest()
        entries.append((name, digest))
    entries.sort()
    payload = repr((str(page), entries)).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _file_content_cache_key(f):
    """Return a stable cache key, avoiding a full read for Streamlit uploads.

    UploadedFile.file_id identifies the uploaded object across normal Streamlit
    reruns. Use it with the filename and size for a fast cache lookup. For
    generated/custom file wrappers without file_id, retain the content digest
    fallback so changed bytes cannot accidentally reuse a stale conversion.
    """
    name = str(getattr(f, 'name', ''))
    file_id = getattr(f, 'file_id', None)
    size = getattr(f, 'size', None)
    if file_id:
        return f"{name}|upload:{file_id}|{size if size is not None else ''}"
    try:
        data = f.getvalue()
    except Exception:
        data = b''
    digest = hashlib.sha256(data).hexdigest()
    return f"{name}|sha256:{digest}"


def _reset_for_new_document_set():
    """Clear all document-derived state when the user loads another file set."""
    st.session_state.results = pd.DataFrame(columns=RESULT_COLUMNS)
    st.session_state.cad_results = pd.DataFrame(columns=RESULT_COLUMNS)
    st.session_state.manual = []
    st.session_state.pdf_pages = {}
    st.session_state.pdf_status = {}
    st.session_state.legend_maps = {}
    st.session_state.xlsx_bytes = None
    # Results and generated exports are document-specific; never show cached
    # route measurements from the previous upload after selecting new files.
    st.session_state.measurements = pd.DataFrame()
    st.session_state['_measure_export_cache'] = None
    st.session_state.pdf_measurements = pd.DataFrame()
    st.session_state.analysis_requested = False
    st.session_state.analysis_running = False
    st.session_state.analysis_last_error = ''
    st.session_state.analysis_events = []
    st.session_state.cad_analysis_requested = False
    st.session_state.cad_last_error = ''
    st.session_state.selected_element_preview_idx = 0
    # Converted DWG/DXF data belongs to the old document set.  Clearing it
    # avoids reusing a conversion from a previous project.
    st.session_state.converted_dxf = {}
    st.session_state.project_name = 'Zestawienie elementów'


def init_state():
    st.session_state.setdefault('results', pd.DataFrame(columns=RESULT_COLUMNS))
    st.session_state.setdefault('manual', [])
    st.session_state.setdefault('patterns', DEFAULT_PATTERNS.copy())
    st.session_state.setdefault('project_name', 'Zestawienie elementów')
    st.session_state.setdefault('pdf_pages', {})
    st.session_state.setdefault('pdf_status', {})
    st.session_state.setdefault('analysis_mode', 'Hybrydowa')
    st.session_state.setdefault('ocr_lang', 'pol+eng')
    st.session_state.setdefault('exclude_legend', False)
    st.session_state.setdefault('pdf_exclude_legend', True)
    st.session_state.setdefault('dxf_count_mode', 'Wg bloków CAD')
    st.session_state.setdefault('auto_dwg_to_dxf', True)
    st.session_state.setdefault('theme_mode', 'dark')
    st.session_state.setdefault('auto_open_result', False)
    st.session_state.setdefault('converted_dxf', {})
    st.session_state.setdefault('xlsx_bytes', None)
    st.session_state.setdefault('measurements', pd.DataFrame())
    st.session_state.setdefault('_measure_export_cache', None)
    st.session_state.setdefault('pdf_measurements', pd.DataFrame())
    st.session_state.setdefault('legend_maps', {})
    st.session_state.setdefault('selected_element_preview_idx', 0)
    st.session_state.setdefault('_loaded_files_fingerprint', None)
    # Przełączanie motywu powoduje pełny rerun Streamlit, ale nie może być
    # traktowane jako zmiana zestawu dokumentów. Flaga jest konsumowana przez
    # app.py podczas najbliższego rerunu.
    st.session_state.setdefault('_skip_document_reset_once', False)
    st.session_state.setdefault('pdf_legend_only', True)

