import base64
from pathlib import Path
from urllib.parse import quote
import streamlit as st
from .config import APP_VERSION

def _asset_b64(path: Path):
    return base64.b64encode(path.read_bytes()).decode('ascii') if path.exists() else ''

# Selektory przycisku-suwaka. Streamlit zmieniał strukturę DOM między wersjami, więc
# stylujemy przycisk kilkoma niezależnymi ścieżkami (każda jako osobna reguła CSS):
#  1) klasa .st-key-<key> (Streamlit >= 1.39),
#  2) sąsiedztwo kontenera z kotwicą przez :has() – nowy i stary układ kontenerów.
_THEME_BTN_KEY = 'theme_slider_action'
_THEME_BTN_SELECTORS = [
    f'.st-key-{_THEME_BTN_KEY} button',
    'div[data-testid="stElementContainer"]:has(.th-theme-control-anchor) + div[data-testid="stElementContainer"] button',
    'div[data-testid="element-container"]:has(.th-theme-control-anchor) + div[data-testid="element-container"] button',
    'div.element-container:has(.th-theme-control-anchor) + div.element-container button',
]

def _svg_uri(svg):
    return 'data:image/svg+xml;utf8,' + quote(svg, safe="/:=,;'() ")

def _sun_svg(color):
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><g fill='{color}' stroke='{color}' "
            "stroke-width='2' stroke-linecap='round'><circle cx='12' cy='12' r='4.5' stroke='none'/>"
            "<path d='M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8'/></g></svg>")

def _moon_svg(color):
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='{color}' "
            "d='M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z'/></svg>")

def _theme_switch_css(dark):
    """Jeden przycisk = cały suwak. Ikony to przezroczyste SVG (bez kwadratowych pól),
    aktywna ikona jest w białym uchwycie (::after), nieaktywna – na torze (tło przycisku)."""
    # Neutralna paleta celowo zbliżona do tła stałego sidebara.
    # Przełącznik nie powinien wyglądać jak osobny niebieski przycisk.
    track = '#1F2937' if dark else '#E2E8F0'
    border = '#475569' if dark else '#CBD5E1'
    # tor: nieaktywna ikona po przeciwnej stronie niż uchwyt
    idle_icon = _svg_uri(_sun_svg('#CBD5E1')) if dark else _svg_uri(_moon_svg('#64748B'))
    idle_pos = '12px center' if dark else '114px center'
    knob_icon = _svg_uri(_moon_svg('#1F2937')) if dark else _svg_uri(_sun_svg('#475569'))
    knob_left = '108px' if dark else '4px'
    out = []
    for sel in _THEME_BTN_SELECTORS:
        out.append(
            f'{sel}{{display:flex!important;width:148px!important;min-width:148px!important;max-width:148px!important;'
            f'height:42px!important;min-height:42px!important;padding:0!important;margin:4px auto 10px!important;'
            f'border-radius:999px!important;box-sizing:border-box!important;position:relative!important;overflow:hidden!important;'
            f'background-color:{track}!important;border:2px solid {border}!important;'
            f'box-shadow:inset 0 1px 3px rgba(0,0,0,.28)!important;color:transparent!important;font-size:0!important;line-height:0!important;'
            f'background-image:url("{idle_icon}")!important;background-repeat:no-repeat!important;'
            f'background-size:22px 22px!important;background-position:{idle_pos}!important;cursor:pointer!important}}\n'
            f'{sel} *{{display:none!important}}\n'
            f'{sel}::after{{content:""!important;position:absolute!important;top:3px!important;left:{knob_left}!important;'
            f'width:32px!important;height:32px!important;border-radius:50%!important;background-color:#fff!important;'
            f'background-image:url("{knob_icon}")!important;background-repeat:no-repeat!important;background-position:center!important;'
            f'background-size:20px 20px!important;box-shadow:0 1px 4px rgba(0,0,0,.38)!important;transition:left .18s ease!important}}\n'
            f'{sel}:hover{{border-color:#93C5FD!important;box-shadow:0 0 0 2px rgba(37,99,235,.22),inset 0 1px 3px rgba(0,0,0,.22)!important}}\n'
            f'{sel}:focus,{sel}:focus-visible{{outline:none!important;border-color:#93C5FD!important}}\n'
        )
    out.append('.th-theme-control-anchor{height:0!important;margin:0!important;padding:0!important;line-height:0!important}\n')
    return ''.join(out)

def apply_theme_css(theme_mode='dark', assets_dir=None):
    assets_dir = Path(assets_dir or Path(__file__).resolve().parent.parent / 'assets')
    dark = theme_mode == 'dark'
    switch_css = _theme_switch_css(dark)
    app_bg = '#111827' if dark else '#E7EBF0'
    card = '#1F2937' if dark else '#FFFFFF'
    text = '#F1F5F9' if dark else '#172033'
    helper = '#94A3B8' if dark else '#64748B'
    border = '#374151' if dark else '#D9E0E7'
    input_bg = '#273449' if dark else '#FFFFFF'
    css = f'''<style>
html,body,[class*="css"]{{font-family:"Segoe UI",Arial,sans-serif!important}}
.stApp{{background:{app_bg}!important;color:{text}!important}}
[data-testid="stHeader"]{{background:{app_bg}!important}}
[data-testid="stToolbar"],footer{{display:none!important}}
.main .block-container{{max-width:1280px!important;margin:0 auto!important;padding:1.1rem 1.5rem 2.5rem!important}}
section[data-testid="stSidebar"]{{display:block!important;visibility:visible!important;opacity:1!important;width:205px!important;min-width:205px!important;max-width:205px!important;transform:none!important;margin-left:0!important;background:linear-gradient(180deg,#172033 0%,#0F172A 100%)!important;border-right:1px solid #263847!important}}
/* Sidebar jest zawsze rozwinięty — ukryj przycisk zwijania i wymuś stałą szerokość. */
[data-testid="stSidebarCollapsedControl"],button[data-testid="stSidebarCollapseButton"]{{display:none!important;visibility:hidden!important}}
[data-testid="stSidebar"]{{position:relative!important;left:0!important}}
section[data-testid="stSidebar"]>div{{width:205px!important;visibility:visible!important}}
.th-brand{{display:flex;flex-direction:column;align-items:center;text-align:center;gap:4px;padding:8px 2px 14px;border-bottom:1px solid #2A3B49;margin-bottom:8px}}
.th-logo-img{{width:58px;height:58px;object-fit:contain;display:block}}
.th-brand-title{{font-size:23px;font-weight:800;letter-spacing:1px;color:#fff!important}}
.th-brand-version{{font-size:11px;color:#C4D0D9!important}}
.th-nav-button{{padding:0;margin:3px 0;border-radius:7px}}
/* Moduły PDF/DWG: w trybie jasnym zachowują dokładnie tę samą bazową
   kolorystykę co w trybie ciemnym. Zielony jest wyłącznie stanem aktywnym DWG. */
section[data-testid="stSidebar"] .th-nav-button button span,
section[data-testid="stSidebar"] .th-nav-button button p{{color:#fff!important}}
section[data-testid="stSidebar"] .th-nav-button button{{width:100%!important;text-align:left!important;min-height:46px!important;padding:9px 10px!important;border:1px solid transparent!important;border-radius:7px!important;background:#30394D!important;color:#fff!important;font-size:14px!important;font-weight:600!important;line-height:1.2!important;box-shadow:none!important}}
section[data-testid="stSidebar"] .th-nav-button button:hover{{background:#3A455C!important;color:#fff!important}}
section[data-testid="stSidebar"] .th-nav-button.active button{{background:#2563EB!important;color:#fff!important;border-color:#2563EB!important;font-weight:700!important}}
section[data-testid="stSidebar"] .th-nav-button.active.th-nav-cad button{{background:#16A34A!important;color:#fff!important;border-color:#16A34A!important;font-weight:700!important}}
/* Streamlit może dodać własny styl kind="secondary"/"primary"; selektor z
   klasą modułu ma pierwszeństwo także w motywie jasnym. */
section[data-testid="stSidebar"] .th-nav-button:not(.active) button[kind="secondary"]{{background:#30394D!important;color:#fff!important;border-color:transparent!important}}
section[data-testid="stSidebar"] .th-nav-button.active.th-nav-pdf button[kind="secondary"],
section[data-testid="stSidebar"] .th-nav-button.active.th-nav-pdf button[kind="primary"]{{background:#2563EB!important;color:#fff!important;border-color:#2563EB!important}}
section[data-testid="stSidebar"] .th-nav-button.active.th-nav-cad button[kind="secondary"],
section[data-testid="stSidebar"] .th-nav-button.active.th-nav-cad button[kind="primary"]{{background:#16A34A!important;color:#fff!important;border-color:#16A34A!important}}
.th-section-title{{margin-top:10px!important;padding:8px 0!important;font-size:18px!important;font-weight:800!important;color:{text}!important}}
.th-help{{margin:2px 0 10px!important;font-size:13px!important;line-height:1.45!important;color:{helper}!important}}
.th-progress{{background:{card}!important;border:1px solid {border}!important;border-radius:9px;padding:12px 14px;margin-top:9px;color:{text}!important}}
.th-progress-steps{{display:flex!important;flex-direction:row!important;align-items:flex-start!important;justify-content:space-between!important;gap:8px!important;width:100%!important;margin-top:8px!important;position:relative!important}}
.th-progress-steps::before{{content:"";position:absolute!important;left:7%!important;right:7%!important;top:12px!important;height:2px!important;background:{border}!important;z-index:0!important}}
.th-step{{flex:1 1 0!important;min-width:0!important;text-align:center!important;color:{helper}!important;font-size:10px!important;line-height:1.2!important;position:relative!important;z-index:1!important}}
.th-step-dot{{width:23px;height:23px;border-radius:50%;background:#E7EBEF;color:#53616D;display:inline-flex;align-items:center;justify-content:center;font-weight:700;margin-bottom:3px}}
.th-step.active .th-step-dot{{background:#2563EB;color:#fff}}
.th-step.done .th-step-dot{{background:#16A34A;color:#fff}}
.th-empty{{margin-top:10px;padding:16px;text-align:center;border:1px dashed {border};border-radius:9px;color:{helper};background:{card}}}
.stButton>button,.stDownloadButton>button{{border-radius:7px!important;min-height:38px!important;font-weight:650!important;border:1px solid {border}!important;background:{input_bg}!important;color:{text}!important}}
.stButton>button:hover,.stDownloadButton>button:hover{{border-color:#2563EB!important}}
button[kind="primary"]{{border-radius:7px!important;font-weight:750!important;min-height:46px!important;background:#16A34A!important;border:0!important;color:#fff!important}}
button[kind="primary"]:hover{{background:#15803D!important}}
[data-testid="stFileUploader"]{{background:{card}!important}}
section[data-testid="stFileUploaderDropzone"]{{background:{card}!important;border:1px dashed {border}!important;border-radius:9px!important;padding:8px!important}}
section[data-testid="stFileUploaderDropzone"] button{{background:#2563EB!important;color:#fff!important;border:1px solid #2563EB!important;border-radius:7px!important;font-size:14px!important;font-weight:700!important;min-height:40px!important;padding:8px 16px!important}}
[data-testid="stFileUploaderFile"] button{{background:transparent!important;color:{helper}!important;border:0!important;min-height:0!important;padding:2px 6px!important}}
.stCheckbox label p,.stRadio label p,.stSelectbox label,.stFileUploader label{{font-size:14px!important;font-weight:600!important;color:{text}!important}}
input,textarea,[data-baseweb="select"]>div{{background:{input_bg}!important;color:{text}!important;border-color:{border}!important}}
input[type="checkbox"],input[type="radio"]{{accent-color:#2563EB!important}}
[data-baseweb="tab"]{{color:{helper}!important;font-weight:600!important}}
[data-baseweb="tab"][aria-selected="true"]{{color:#2563EB!important}}
[data-testid="stDataFrame"]{{border:1px solid {border}!important;border-radius:9px!important;overflow:hidden!important}}
/* W trybie jasnym wymuś czytelny, ciemny tekst także dla elementów Streamlit,
   które mają własne kolory i potrafią odziedziczyć jasny tekst z motywu bazowego. */
.stMarkdown, .stMarkdown p, .stMarkdown li, .stCaption, [data-testid="stCaptionContainer"],
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary p,
[data-testid="stExpander"] [data-testid="stMarkdownContainer"],
[data-testid="stAlert"] p, [data-testid="stAlert"] span,
[data-testid="stFileUploader"] section, [data-testid="stFileUploader"] section p,
[data-testid="stFileUploader"] small, .stSelectbox label, .stMultiSelect label,
.stTextInput label, .stNumberInput label, .stTextArea label, .stDateInput label,
.stTimeInput label, .stSlider label, .stCheckbox label, .stRadio label{{color:{text}!important}}
[data-testid="stExpander"] summary{{color:{text}!important}}
[data-testid="stExpander"] summary:hover{{color:#0F172A!important}}
input::placeholder, textarea::placeholder{{color:{helper}!important;opacity:1!important}}
[data-baseweb="select"] span{{color:{text}!important}}
[data-baseweb="select"] input{{color:{text}!important}}
[data-testid="stDataEditor"]{{color:{text}!important}}
.th-card{{background:{card};border:1px solid {border};border-radius:9px;padding:12px 14px;color:{text}}}
.th-card-title{{font-size:12px;font-weight:700;color:{helper}}}
.th-card-value{{font-size:24px;font-weight:800;color:{text}}}
.th-footer{{display:flex;gap:18px;margin-top:16px;color:{helper}!important;font-size:12px!important}}
.th-theme-icon{{display:none!important}}
/* Zintegrowany przełącznik motywu — jeden przycisk (reguły generowane w _theme_switch_css). */
{switch_css}
[aria-label*="keyboard_double"],[title*="keyboard_double"],[data-testid*="keyboard_double"]{{font-size:0!important;overflow:hidden!important}}
button[data-testid="stSidebarCollapseButton"] span.material-symbols-rounded,button[data-testid="stSidebarCollapseButton"] span[class*="material-symbol"]{{font-size:0!important}}
@keyframes thanos-xlsx-ready-pulse{{0%,100%{{box-shadow:0 0 0 0 rgba(22,163,74,.10)}}50%{{box-shadow:0 0 0 6px rgba(22,163,74,.32)}}}}
div[data-testid="stDownloadButton"] button.th-xlsx-ready{{animation:thanos-xlsx-ready-pulse 1.25s ease-in-out infinite!important;background:#16A34A!important;border-color:#16A34A!important;color:#fff!important}}
</style>'''
    st.markdown(css, unsafe_allow_html=True)

def render_sidebar_and_theme(app_version=APP_VERSION):
    with st.sidebar:
        logo_path = Path(__file__).resolve().parent.parent / 'assets' / 'G.jpg'
        if logo_path.exists():
            logo_b64 = _asset_b64(logo_path)
            st.markdown(f'<div class="th-brand"><img class="th-logo-img" src="data:image/jpeg;base64,{logo_b64}" alt="THANOS"><div class="th-brand-title">THANOS</div><div class="th-brand-version">{app_version}</div></div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="th-brand"><div class="th-brand-title">THANOS</div><div class="th-brand-version">{app_version}</div></div>', unsafe_allow_html=True)

        # Zintegrowany przełącznik motywu: kliknięcie samego suwaka zmienia tryb.
        # Nie ma osobnego przycisku ani napisu „Przełącz tryb”.
        current_dark = st.session_state.get('theme_mode', 'dark') == 'dark'
        # Jeden przycisk = cały suwak. Ikony są częścią jego tła CSS, więc nie mają osobnych ramek.
        st.markdown('<div class="th-theme-control-anchor"></div>', unsafe_allow_html=True)
        if st.button(' ', key=_THEME_BTN_KEY, help='Przełącz tryb jasny / ciemny', use_container_width=False):
            # Zmiana motywu wymaga rerunu, ale nie może wyzerować wyników analizy.
            # app.py skonsumuje tę jednorazową flagę przed kontrolą fingerprintu plików.
            st.session_state['_skip_document_reset_once'] = True
            st.session_state.theme_mode = 'light' if current_dark else 'dark'
            st.rerun()

        nav_items=[('pdf','▣','Analiza PDF'),('cad','⇄','Analiza DWG/DXF'),('cables','⌁','Lista kablowa'),('profile','⚙','Profil projektu')]
        for key,icon,label in nav_items:
            active=st.session_state.get('ui_page','pdf')==key
            module_cls=f'th-nav-{key}'
            state_cls='active' if active else ''
            st.markdown(f'<div class="th-nav-button {module_cls} {state_cls}">', unsafe_allow_html=True)
            if st.button(f'{icon}  {label}',key=f'nav_{key}',use_container_width=True,type=('primary' if active else 'secondary')):
                st.session_state.ui_page=key
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
