import time
import math
import io
import hashlib
import re
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import streamlit as st


from thanos.config import APP_VERSION, DEFAULT_PATTERNS, RESULT_COLUMNS
from thanos.state import init_state, _uploaded_files_fingerprint, _reset_for_new_document_set
from thanos.pdf import (find_tesseract, analyze_pdf, measure_pdf_routes, detect_pdf_scale, measure_pdf_odgrom_uziom, measurable_pdf_odgrom_layers)
from thanos.cad import (find_oda_converter, auto_convert_dwg_to_dxf, get_dxf_files_with_auto_conversion, analyze_dxf, measure_dxf_lengths, count_dxf_inserts_by_layer, measure_lan_ladder_csv, audit_lan_ladder_csv_dxf, validate_measurement_sources, measure_odgrom_uziom, render_selected_insert_png, _legend_display_name_map, collect_legend_graphics, measurable_dxf_layers, audit_dxf_model_layout)
from thanos.state import _file_content_cache_key
from thanos.export import export_xlsx, apply_legend_names_to_results, project_json, build_route_measurement_exports
from thanos.ath import load_excel as ath_load_excel, parse_ath_template, suggest_mapping, generate_ath, catalog_dataframe
from thanos.ui_state import resolve_active_module
from thanos.cable_list import CABLE_COLUMNS, extract_cable_candidates, export_cable_list_xlsx, measure_dxf_cable_routes, measure_cablofil_csv_routes, import_cable_list_table, suggest_cable_route_matches, estimate_cable_paths_without_route_layers
from thanos.project_profile import inspect_dxf, inspect_pdf, build_profile
from thanos.cablofil_geometry import analyze_cablofil_blocks


def _xlsx_filename(base_name: str) -> str:
    """Return an XLSX filename with the current local date and time."""
    base = Path(str(base_name or "Thanos")).stem
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{base}_{stamp}.xlsx"


def _render_model_layout_scope_audit(file_obj, key_prefix: str):
    """Render non-destructive Model Space / Layout scope audit for a selected DXF.

    Use the stable upload identity for UI state keys. Hashing all bytes on every
    Streamlit rerun is expensive for large DXF files and is unnecessary when
    UploadedFile.file_id is available.
    """
    content_key = _file_content_cache_key(file_obj)
    digest = hashlib.sha1(content_key.encode('utf-8', errors='replace')).hexdigest()[:12]
    base_key = f"{key_prefix}_scope_{digest}"
    with st.expander('🔎 Kontrola zakresu DXF — Model / Layout', expanded=False):
        st.caption('Porównuje encje Model Space z obszarami rzutni Layout. Raport jest pomocniczy i nie zmienia automatycznie ilości w przedmiarze.')
        if st.button('Analizuj Model Space i Layouty', key=f'{base_key}_run', use_container_width=True):
            try:
                with st.spinner(f'Analizuję Model Space i Layouty: {file_obj.name}…'):
                    result = audit_dxf_model_layout(file_obj.name, file_obj.getvalue())
                st.session_state[f'{base_key}_result'] = result
                st.success('Analiza zakresu zakończona.')
            except Exception as exc:
                st.error(f'Błąd analizy zakresu: {type(exc).__name__}: {exc}')
        result = st.session_state.get(f'{base_key}_result')
        if result:
            st.json(result.get('summary', {}))
            st.markdown('**Layouty i rzutnie**')
            st.dataframe(result.get('layouts', pd.DataFrame()), use_container_width=True, hide_index=True)
            st.markdown('**Podsumowanie typów encji w modelu**')
            st.dataframe(result.get('types', pd.DataFrame()), use_container_width=True, hide_index=True)
            entities = result.get('entities', pd.DataFrame())
            if not entities.empty and 'Status zakresu' in entities.columns:
                outside = entities[entities['Status zakresu'].eq('POZA_OBSZAREM_RZUTNI')]
                unknown = entities[entities['Status zakresu'].eq('NIEJEDNOZNACZNE')]
                if not outside.empty:
                    st.warning(f'Kandydaci poza obszarem rzutni: {len(outside)}. Zweryfikuj przed wykluczeniem.')
                    st.dataframe(outside, use_container_width=True, hide_index=True)
                if not unknown.empty:
                    st.info(f'Encje bez rozpoznanej obwiedni: {len(unknown)}; wymagają osobnej oceny.')
            st.dataframe(entities, use_container_width=True, hide_index=True)
            # User-controlled, non-destructive layer exclusions. They are saved per
            # file content fingerprint and only applied by modules that support it.
            layer_key = f'{base_key}_excluded_layers'
            if not entities.empty and 'Warstwa' in entities.columns:
                layer_rows = []
                for layer_name, grp in entities.groupby('Warstwa', dropna=False):
                    layer_name = str(layer_name or '0')
                    outside_n = int(grp['Status zakresu'].eq('POZA_OBSZAREM_RZUTNI').sum()) if 'Status zakresu' in grp else 0
                    unknown_n = int(grp['Status zakresu'].isin(['NIEJEDNOZNACZNE','BRAK_RZUTNI']).sum()) if 'Status zakresu' in grp else 0
                    layer_rows.append({'Warstwa': layer_name, 'Encje': len(grp), 'Poza rzutniami': outside_n,
                                       'Niepewne': unknown_n, 'Wyklucz warstwę z pomiaru': False})
                layer_df = pd.DataFrame(layer_rows).sort_values(['Poza rzutniami','Niepewne','Warstwa'], ascending=[False,False,True])
                prior = set(st.session_state.get(layer_key, []))
                if prior:
                    layer_df['Wyklucz warstwę z pomiaru'] = layer_df['Warstwa'].isin(prior)
                st.markdown('**Decyzje zakresu — wykluczanie całych warstw**')
                st.caption('Wykluczenie działa na całą warstwę, nie na pojedyncze encje. Używaj tylko po sprawdzeniu raportu; decyzja nie usuwa danych z DXF.')
                edited = st.data_editor(layer_df, key=f'{base_key}_layer_editor', use_container_width=True, hide_index=True,
                    disabled=['Warstwa','Encje','Poza rzutniami','Niepewne'], column_config={
                        'Wyklucz warstwę z pomiaru': st.column_config.CheckboxColumn('Wyklucz warstwę z pomiaru', help='Zastosowanie dostępne w modułach pomiarowych.')})
                st.session_state[layer_key] = edited.loc[edited['Wyklucz warstwę z pomiaru'], 'Warstwa'].astype(str).tolist()
                if st.session_state[layer_key]:
                    st.warning('Wykluczone warstwy: ' + ', '.join(st.session_state[layer_key]))
            st.download_button('Pobierz raport zakresu DXF (CSV)', entities.to_csv(index=False).encode('utf-8-sig'),
                file_name=f'Thanos_kontrola_zakresu_{Path(file_obj.name).stem}.csv', mime='text/csv', key=f'{base_key}_download')
from thanos.ui_theme import apply_theme_css, render_sidebar_and_theme

st.set_page_config(page_title='Thanos', page_icon='📐', layout='wide', initial_sidebar_state='expanded')
init_state()
apply_theme_css(st.session_state.get('theme_mode','dark'), Path(__file__).resolve().parent/'assets')
render_sidebar_and_theme(APP_VERSION)

# Project-profile wizard: discover project-specific conventions before takeoff.
if st.session_state.get('ui_page') == 'profile':
    st.markdown('<div class="th-section-title">⚙ &nbsp; Uniwersalny profil projektu</div>', unsafe_allow_html=True)
    st.markdown('#### Wczytaj istniejący profil')
    _profile_upload = st.file_uploader('Profil projektu (JSON)', type=['json'], accept_multiple_files=False, key='project_profile_json_upload')
    if _profile_upload is not None and st.button('📂 Wczytaj i zastosuj profil JSON', key='project_profile_json_apply', use_container_width=True):
        try:
            _loaded_profile = json.loads(_profile_upload.getvalue().decode('utf-8-sig'))
            _required_profile_keys = {'nazwa_projektu', 'warstwy_tras_kablowych', 'warstwy_urządzeń', 'jednostki_CAD', 'parametry_pomiaru'}
            if not isinstance(_loaded_profile, dict) or not _required_profile_keys.issubset(_loaded_profile):
                raise ValueError('Plik nie zawiera wymaganych pól profilu projektu.')
            if not isinstance(_loaded_profile.get('warstwy_tras_kablowych'), list) or not isinstance(_loaded_profile.get('warstwy_urządzeń'), list):
                raise ValueError('Pola warstw tras i urządzeń muszą być listami.')
            _params = _loaded_profile.get('parametry_pomiaru') or {}
            _height = float(_params.get('wysokość_pomieszczenia_m', 3.0))
            _reserve = float(_params.get('zapas_kabla_proc', 10.0))
            if not (0 <= _height <= 30 and 0 <= _reserve <= 100):
                raise ValueError('Wysokość lub zapas kabla są poza dozwolonym zakresem.')
            _unit = str(_loaded_profile.get('jednostki_CAD', 'auto')).casefold()
            if _unit not in {'auto', 'mm', 'cm', 'm', 'automatycznie / wg pliku'}:
                raise ValueError(f'Nieobsługiwane jednostki CAD: {_unit}')
            st.session_state['thanos_project_profile'] = _loaded_profile
            st.session_state['profile_project_name'] = str(_loaded_profile.get('nazwa_projektu', 'Nowy projekt'))
            st.session_state['profile_units'] = {'auto':'Automatycznie / wg pliku','automatycznie / wg pliku':'Automatycznie / wg pliku','mm':'mm','cm':'cm','m':'m'}[_unit]
            st.session_state['profile_height'] = _height
            st.session_state['profile_reserve'] = _reserve
            st.session_state['cable_room_height'] = _height
            st.session_state['cable_reserve_pct'] = _reserve
            st.success(f"Profil „{_loaded_profile.get('nazwa_projektu', 'bez nazwy')}” wczytany. Warstwy tras: {len(_loaded_profile['warstwy_tras_kablowych'])}; warstwy urządzeń: {len(_loaded_profile['warstwy_urządzeń'])}.")
        except Exception as _profile_exc:
            st.error(f'Nie udało się wczytać profilu: {type(_profile_exc).__name__}: {_profile_exc}')
    st.markdown('<div class="th-help">Wczytaj rysunki przykładowe. Thanos zbierze warstwy, bloki, teksty, typy kabli i możliwe oznaczenia obwodów. Sugestie nie zmieniają automatycznie przedmiaru — zatwierdzasz je przed zapisaniem profilu.</div>', unsafe_allow_html=True)
    profile_files = st.file_uploader('Rysunki do rozpoznania', type=['dxf','pdf'], accept_multiple_files=True, key='project_profile_files') or []
    c1,c2,c3 = st.columns(3)
    with c1: profile_name = st.text_input('Nazwa projektu', value='Nowy projekt', key='profile_project_name')
    with c2: profile_units = st.selectbox('Jednostki rysunków CAD', ['Automatycznie / wg pliku','mm','cm','m'], key='profile_units')
    with c3: profile_height = st.number_input('Wysokość pomieszczenia [m]', min_value=0.0, max_value=30.0, value=3.0, step=0.1, key='profile_height')
    profile_reserve = st.number_input('Zapas kabla [%]', min_value=0.0, max_value=100.0, value=10.0, step=1.0, key='profile_reserve')
    if st.button('🔎 Przeanalizuj rysunki i zaproponuj profil', type='primary', use_container_width=True, key='profile_analyze'):
        _analyses=[]; _errors=[]
        with st.spinner('Analizuję warstwy i oznaczenia…'):
            for _f in profile_files:
                try:
                    _raw=_f.getvalue(); _ext=Path(_f.name).suffix.lower()
                    if _ext == '.dxf':
                        _dxf_analysis = inspect_dxf(_f.name, _raw)
                        try:
                            _dxf_analysis['diagnostyka_cablofil'] = analyze_cablofil_blocks(_f.name, _raw)
                        except Exception as _cab_exc:
                            _dxf_analysis['diagnostyka_cablofil_błąd'] = f'{type(_cab_exc).__name__}: {_cab_exc}'
                        _analyses.append(_dxf_analysis)
                    elif _ext == '.pdf': _analyses.append(inspect_pdf(_f.name, _raw))
                except Exception as _e: _errors.append(f'{_f.name}: {type(_e).__name__}: {_e}')
        st.session_state['profile_analyses'] = _analyses
        st.session_state['profile_errors'] = _errors
        st.session_state['profile_route_layers'] = sorted({x['Warstwa'] for a in _analyses if a.get('rola_rysunku') == 'plan' for x in a.get('warstwy',[]) if str(x.get('Sugestia','')).startswith('Kandydat:')})
        st.session_state['profile_device_layers'] = sorted({x['Warstwa'] for a in _analyses if a.get('rola_rysunku') == 'plan' for x in a.get('warstwy',[]) if x.get('Sugestia') == 'Urządzenia / symbole — kandydat'})
    for _e in st.session_state.get('profile_errors', []): st.warning(_e)
    _analyses=st.session_state.get('profile_analyses', [])
    if _analyses:
        st.success(f'Analiza zakończona: {len(_analyses)} plików. Sprawdź i zatwierdź wykryte ustawienia.')
        for _a in _analyses:
            with st.expander(f"{_a.get('plik','Plik')} — {_a.get('format','')} ", expanded=False):
                if _a.get('ostrzeżenie'): st.warning(_a['ostrzeżenie'])
                if _a.get('format') == 'PDF':
                    st.markdown('**Diagnostyka wektorowa PDF — pomoc do dopasowania z DXF**')
                    _diag_rows = _a.get('diagnostyka_wektorowa') or []
                    if _diag_rows:
                        st.caption('Obwiednia i liczba elementów opisują zawartość wektorową strony. Nie są same w sobie pomiarem tras ani automatycznym potwierdzeniem zgodności współrzędnych PDF–DXF.')
                        _pdf_diag = pd.DataFrame([{ 'Strona': q.get('strona'), 'Szerokość [pkt]': q.get('szerokość_pkt'),
                            'Wysokość [pkt]': q.get('wysokość_pkt'), 'Rysunki wektorowe': q.get('rysunki_wektorowe'),
                            'Obwiednia wektorów [pkt]': str(q.get('obwiednia_wektorów_pkt') or ''),
                            'Rozpoznane opisy koryt/wysokości': ', '.join(q.get('etykiety_koryt_i_wysokości', [])[:12]) }
                            for q in _diag_rows])
                        st.dataframe(_pdf_diag, use_container_width=True, hide_index=True)
                        # Render the PDF page itself so the user can visually compare the route layout.
                        for _q in _diag_rows:
                            _preview_b64 = _q.get('podgląd_jpeg_base64')
                            if _preview_b64:
                                import base64 as _base64
                                st.image(_base64.b64decode(_preview_b64),
                                         caption=f"Podgląd strony PDF {_q.get('strona')} — obraz pomocniczy do porównania z DXF",
                                         use_container_width=True)
                            _yellow_info = _q.get('kandydaci_żółtych_tras', {})
                            if _yellow_info.get('liczba_pikseli', 0):
                                st.caption(f"Kandydackie elementy wektorowe w kolorach żółtym/pomarańczowym: {_yellow_info.get('liczba_pikseli')}; {_yellow_info.get('udział_procentowy', 0)}% rysunków wektorowych; obwiednia: {_yellow_info.get('obwiednia_px')}. To wskazanie wizualne, nie pomiar długości.")
                            _route_overlay = _q.get('nakładka_kandydatów_tras_base64')
                            if _route_overlay:
                                import base64 as _base64
                                with st.expander('Pokaż nakładkę wykrytych kandydatów tras (różowe zaznaczenie)', expanded=False):
                                    st.image(_base64.b64decode(_route_overlay), caption='Różowe linie = żółte/pomarańczowe ścieżki wektorowe PDF; mogą obejmować też symbole i fragmenty opisów.', use_container_width=True)
                                    st.warning('Nakładka jest oparta na kolorach ścieżek wektorowych PDF. Nie wyznacza jeszcze osi tras, nie dopasowuje ich do DXF i nie wylicza długości.')
                            _axes = _q.get('wstępne_osie_tras') or []
                            if _axes:
                                st.markdown('**Wstępne osie tras po scaleniu równoległych krawędzi**')
                                st.caption('Algorytm scala tylko bliskie osie (tolerancja 4 pkt), łączy fragmenty z przerwą do 2 pkt i pomija osie krótsze niż 60 pkt, które często pochodzą z opisów lub ramek. To nadal przybliżenie diagnostyczne w punktach PDF — trzeba je porównać z DXF; nie jest to przedmiar.')
                                st.dataframe(pd.DataFrame(_axes[:100]), use_container_width=True, hide_index=True)
                            _segments = _q.get('odcinki_kandydackie') or []
                            if _segments:
                                st.markdown('**Długie odcinki wektorowe — diagnostyka, nie przedmiar**')
                                st.caption('Odcinki poniżej są wyciągnięte z kolorowych ścieżek PDF. Koryta mogą być obrysowane dwiema równoległymi liniami, dlatego nie sumuj tych wartości jako długości tras.')
                                st.dataframe(pd.DataFrame(_segments[:80]), use_container_width=True, hide_index=True)
                        _pdf_labels = sorted({label for q in _diag_rows for label in q.get('etykiety_koryt_i_wysokości', [])})
                        if _pdf_labels:
                            st.write('Rozpoznane etykiety tras:', ', '.join(_pdf_labels[:60]))
                        else:
                            st.info('PDF ma diagnostykę wektorową, ale nie rozpoznano etykiet koryt w warstwie tekstowej. Geometrię można nadal porównywać wizualnie.')
                    else:
                        st.warning('Diagnostyka wektorowa nie została zwrócona dla tego pliku. Sprawdź instalację PyMuPDF (fitz); odczyt tekstu PDF działa niezależnie, ale pomiaru ani dopasowania tras nie wykonano.')
                _cab = _a.get('diagnostyka_cablofil')
                if _cab:
                    st.markdown('**Diagnostyka geometrii bloków Cablofil — kandydaci osi**')
                    st.caption('Thanos odczytuje najdłuższą linię na warstwie CP_DETAL z definicji każdego bloku i przelicza jej końce do współrzędnych Model Space. Wynik nie jest jeszcze przedmiarem; odcinki nakładające się i duplikaty wymagają sprawdzenia.')
                    _cab_cols = st.columns(4)
                    _cab_cols[0].metric('Wstawienia Cablofil', _cab.get('liczba_wstawień', 0))
                    _cab_cols[1].metric('Kandydaci osi', _cab.get('liczba_osi_z_geometrii', 0))
                    _cab_cols[2].metric('Bez osi', _cab.get('liczba_bez_osi', 0))
                    _cab_cols[3].metric('Jednostki DXF', _cab.get('jednostki_opis', 'nieznane'))
                    st.dataframe(pd.DataFrame(_cab.get('typy', [])), use_container_width=True, hide_index=True)
                    st.markdown('**Podsumowanie diagnostyczne — bez automatycznego odejmowania nakładających się tras**')
                    _cab_sum = _cab.get('podsumowanie_diagnostyczne', [])
                    if _cab_sum:
                        st.dataframe(pd.DataFrame(_cab_sum), use_container_width=True, hide_index=True)
                    _cab_m1, _cab_m2 = st.columns(2)
                    _cab_m1.metric('Osi unikalnych geometrycznie', _cab.get('liczba_osi_unikalnych_geometrycznie', 0))
                    _cab_m2.metric('Suma po deduplikacji identycznych osi [jedn. CAD]', _cab.get('suma_długości_po_usunięciu_ident_dubl_jedn_CAD', 0))
                    st.dataframe(pd.DataFrame(_cab.get('elementy', [])), use_container_width=True, hide_index=True)
                    st.info(_cab.get('uwaga', 'Wynik diagnostyczny — nie jest zatwierdzonym przedmiarem.'))
                    st.download_button('Pobierz diagnostykę geometrii Cablofil (CSV)', data=pd.DataFrame(_cab.get('elementy', [])).to_csv(index=False).encode('utf-8-sig'), file_name=f"Cablofil_geometria_{Path(_a.get('plik','rysunek')).stem}.csv", mime='text/csv', key='cab_csv_'+hashlib.sha1(_a.get('plik','').encode()).hexdigest()[:8])
                elif _a.get('diagnostyka_cablofil_błąd'):
                    st.warning('Nie udało się wykonać diagnostyki Cablofil: ' + _a['diagnostyka_cablofil_błąd'])
                if _a.get('warstwy'):
                    st.dataframe(pd.DataFrame(_a['warstwy']), use_container_width=True, hide_index=True)
                    _layer_names = [str(r.get('Warstwa','')) for r in _a.get('warstwy',[]) if r.get('Linie/krzywe',0)]
                    if _layer_names and _a.get('geometria_podgladu'):
                        _geom_key = 'profile_geom_'+hashlib.sha1(_a.get('plik','').encode()).hexdigest()[:8]
                        _sel_layers = st.multiselect('Porównaj geometrię warstw (możesz wybrać kilka)', _layer_names,
                                                     default=[_layer_names[0]] if _layer_names else [], key=_geom_key)
                        _zoom_fit = st.checkbox('Dopasuj widok do całej wybranej geometrii', value=True,
                                                key=_geom_key+'_fit')
                        _max_per_layer = st.slider('Maksymalna liczba elementów na warstwę', 100, 2500, 700, 100,
                                                   key=_geom_key+'_limit')
                        if _sel_layers:
                            try:
                                import matplotlib.pyplot as _plt
                                _fig, _ax = _plt.subplots(figsize=(12, 6))
                                _all_x, _all_y = [], []
                                _total_drawn = 0
                                for _sel_layer in _sel_layers:
                                    _segments = _a['geometria_podgladu'].get(_sel_layer, [])
                                    _layer_drawn = False
                                    for _seg in _segments[:_max_per_layer]:
                                        _pts = _seg.get('punkty', [])
                                        if len(_pts) >= 2:
                                            _xs = [p[0] for p in _pts]; _ys = [p[1] for p in _pts]
                                            _ax.plot(_xs, _ys, linewidth=0.7, label=_sel_layer if not _layer_drawn else None)
                                            _all_x.extend(_xs); _all_y.extend(_ys); _total_drawn += 1
                                            _layer_drawn = True
                                if _all_x and _all_y:
                                    if _zoom_fit:
                                        _xmin, _xmax = min(_all_x), max(_all_x); _ymin, _ymax = min(_all_y), max(_all_y)
                                        _dx=max(_xmax-_xmin, 1.0); _dy=max(_ymax-_ymin, 1.0)
                                        _ax.set_xlim(_xmin-_dx*.03, _xmax+_dx*.03)
                                        _ax.set_ylim(_ymin-_dy*.03, _ymax+_dy*.03)
                                    _ax.set_aspect('equal', adjustable='box')
                                _ax.set_title('Porównanie geometrii: '+', '.join(_sel_layers))
                                if len(_sel_layers) > 1:
                                    _ax.legend(loc='best', fontsize='small')
                                _ax.grid(True, alpha=.2)
                                st.pyplot(_fig, use_container_width=True); _plt.close(_fig)
                                st.caption(f'Podgląd Model Space: narysowano { _total_drawn } elementów. To podgląd geometrii, nie zatwierdzony pomiar długości.')
                            except Exception as _preview_err:
                                st.info(f'Nie udało się wyświetlić podglądu graficznego: {_preview_err}')
                if _a.get('bloki'): st.dataframe(pd.DataFrame(_a['bloki']), use_container_width=True, hide_index=True)
                _patterns=_a.get('wzorce',{})
                st.write('Wykryte typy kabli:', ', '.join(_patterns.get('typy_kabli',[])) or 'brak w tekście')
                st.write('Przykładowe oznaczenia obwodów:', ', '.join(_patterns.get('oznaczenia_obwodów',[])[:35]) or 'brak w tekście')
                _samples=_a.get('próbki_tekstu',[])
                if _samples: st.text_area('Próbki tekstu źródłowego', '\n'.join(_samples[:30]), height=160, key='profile_samples_'+hashlib.sha1(_a.get('plik','').encode()).hexdigest()[:8], disabled=True)
        _all_layers=sorted({x['Warstwa'] for a in _analyses for x in a.get('warstwy',[])})
        st.markdown('#### Potwierdź reguły dla tego projektu')
        st.caption('Thanos rozróżnia plan od schematu. Warstwy opisowe i ramki nie są proponowane jako trasy, a geometria schematu nie jest traktowana jako fizyczna trasa. Wszystkie sugestie wymagają potwierdzenia.')
        _route_layers=st.multiselect('Warstwy tras kablowych', _all_layers, default=[x for x in st.session_state.get('profile_route_layers',[]) if x in _all_layers], key='profile_route_layers_confirm')
        _device_layers=st.multiselect('Warstwy urządzeń / punktów końcowych', _all_layers, default=[x for x in st.session_state.get('profile_device_layers',[]) if x in _all_layers], key='profile_device_layers_confirm')
        _units={'Automatycznie / wg pliku':'auto','mm':'mm','cm':'cm','m':'m'}.get(profile_units,'auto')
        _profile=build_profile(profile_name, _analyses, _route_layers, _device_layers, _units, float(profile_height), float(profile_reserve))
        st.download_button('⬇ Zapisz profil projektu (JSON)', data=json.dumps(_profile,ensure_ascii=False,indent=2).encode('utf-8'), file_name='profil_projektu.json', mime='application/json', use_container_width=True, key='profile_download')
        st.json({'Liczba plików':len(_analyses),'Warstwy tras':_route_layers,'Warstwy urządzeń':_device_layers,'Typy kabli':sorted({x for a in _analyses for x in a.get('wzorce',{}).get('typy_kabli',[])}),'Oznaczenia obwodów':sum(len(a.get('wzorce',{}).get('oznaczenia_obwodów',[])) for a in _analyses)})
    else:
        st.info('Wybierz co najmniej jeden plik DXF lub PDF, a następnie uruchom analizę.')
    st.markdown('<div class="th-footer"><span>Thanos v' + APP_VERSION + '</span><span>Autor aplikacji: Paweł Świerk</span></div>', unsafe_allow_html=True)
    st.stop()

# Dedicated cable-list workspace. It can start from files handed off by either
# PDF or CAD/DXF mode, while remaining independently accessible from the sidebar.
if st.session_state.get('ui_page') == 'cables':
    st.markdown('<div class="th-section-title">⌁ &nbsp; Lista kablowa ze schematów rozdzielnic</div>', unsafe_allow_html=True)
    st.markdown('<div class="th-help">Wczytaj schematy PDF lub DWG/DXF. Thanos wyszukuje oznaczenia kabli i tworzy roboczą listę do weryfikacji. Długości tras nie są zgadywane — uzupełnij je na podstawie pomiarów.</div>', unsafe_allow_html=True)
    st.markdown('#### 1. Wybierz sposób pracy')
    st.caption('Masz już listę? Wczytaj ją poniżej. Zaczynasz od zera? Dodaj rysunki i rozpoznaj oznaczenia kabli.')
    staged = st.session_state.get('cable_handoff_files', []) or []
    if staged:
        st.success('Przekazano pliki z istniejącego modułu: ' + ', '.join(getattr(f, 'name', 'plik') for f in staged))
    st.markdown('**A. Mam już listę kablową**')
    _existing_list_upload = st.file_uploader('Wybierz listę Excel lub CSV', type=['xlsx','xls','csv'], accept_multiple_files=False, key='cable_existing_list_upload')
    if _existing_list_upload is not None and st.button('📥 Wczytaj listę kablową z pliku', key='cable_existing_list_import', use_container_width=True):
        try:
            _imported_list = import_cable_list_table(_existing_list_upload.getvalue(), _existing_list_upload.name)
            st.session_state['cable_list_df'] = _imported_list
            st.session_state['cable_list_edited_df'] = _imported_list.copy()
            st.session_state['cable_list_source_names'] = [_existing_list_upload.name]
            st.session_state['cable_list_errors'] = []
            st.session_state.pop('cable_list_editor', None)
            st.success(f"Wczytano {_existing_list_upload.name}: {len(_imported_list)} pozycji. Brakujące długości pozostały puste.")
            st.rerun()
        except Exception as _list_import_exc:
            st.error(f'Nie udało się wczytać listy kablowej: {type(_list_import_exc).__name__}: {_list_import_exc}')
    st.markdown('**B. Utwórz listę z rysunków**')
    uploaded = st.file_uploader('Wybierz schematy PDF / DWG / DXF', type=['pdf','dwg','dxf'], accept_multiple_files=True, key='cable_list_upload') or []

    # Integrated project-profile workflow, available without leaving Lista kablowa.
    with st.expander('⚙ Ustawienia zaawansowane — profil projektu i pomiar tras', expanded=False):
        st.caption('Dodaj przede wszystkim rzuty tras w DXF oraz, pomocniczo, schematy rozdzielnic PDF. Thanos zaproponuje warstwy; przed zastosowaniem profilu zatwierdź wybór. Sam profil nie przypisuje automatycznie pełnych długości kabli do obwodów.')
        _inline_profile_files = st.file_uploader('Rysunki do rozpoznania profilu (PDF / DXF)', type=['pdf','dxf'], accept_multiple_files=True, key='cable_inline_profile_files') or []
        _inline_profile_cols = st.columns(3)
        with _inline_profile_cols[0]:
            _inline_profile_name = st.text_input('Nazwa projektu', value=str(st.session_state.get('profile_project_name','Nowy projekt')), key='cable_inline_profile_name')
        with _inline_profile_cols[1]:
            _inline_profile_units_label = st.selectbox('Jednostki rysunków CAD', ['Automatycznie / wg pliku','mm','cm','m'], index=0, key='cable_inline_profile_units')
        with _inline_profile_cols[2]:
            _inline_profile_height = st.number_input('Wysokość pomieszczenia [m]', min_value=0.0, max_value=30.0, value=float(st.session_state.get('cable_room_height',3.0)), step=0.1, key='cable_inline_profile_height')
        _inline_profile_reserve = st.number_input('Zapas kabla [%]', min_value=0.0, max_value=100.0, value=float(st.session_state.get('cable_reserve_pct',10.0)), step=1.0, key='cable_inline_profile_reserve')
        if st.button('🔎 Przeanalizuj rysunki i zaproponuj profil', type='primary', use_container_width=True, key='cable_inline_profile_analyze', disabled=not _inline_profile_files):
            _inline_analyses, _inline_errors = [], []
            with st.spinner('Analizuję warstwy, geometrię i oznaczenia…'):
                for _pf in _inline_profile_files:
                    try:
                        _praw = _pf.getvalue()
                        if Path(_pf.name).suffix.lower() == '.dxf':
                            _pa = inspect_dxf(_pf.name, _praw)
                            try:
                                _pa['diagnostyka_cablofil'] = analyze_cablofil_blocks(_pf.name, _praw)
                            except Exception as _pce:
                                _pa['diagnostyka_cablofil_błąd'] = f'{type(_pce).__name__}: {_pce}'
                            _inline_analyses.append(_pa)
                        elif Path(_pf.name).suffix.lower() == '.pdf':
                            _inline_analyses.append(inspect_pdf(_pf.name, _praw))
                    except Exception as _pie:
                        _inline_errors.append(f'{_pf.name}: {type(_pie).__name__}: {_pie}')
            st.session_state['cable_inline_profile_analyses'] = _inline_analyses
            st.session_state['cable_inline_profile_errors'] = _inline_errors
            st.session_state['cable_inline_profile_files_names'] = [f.name for f in _inline_profile_files]
            st.session_state['cable_inline_profile_route_suggestions'] = sorted({str(x.get('Warstwa')) for a in _inline_analyses if a.get('rola_rysunku') == 'plan' for x in a.get('warstwy',[]) if str(x.get('Sugestia','')).startswith('Kandydat:') and x.get('Warstwa')})
            st.session_state['cable_inline_profile_device_suggestions'] = sorted({str(x.get('Warstwa')) for a in _inline_analyses if a.get('rola_rysunku') == 'plan' for x in a.get('warstwy',[]) if x.get('Sugestia') == 'Urządzenia / symbole — kandydat' and x.get('Warstwa')})
        for _ie in st.session_state.get('cable_inline_profile_errors', []):
            st.warning(_ie)
        _inline_analyses = st.session_state.get('cable_inline_profile_analyses', [])
        if _inline_analyses:
            st.success(f"Analiza profilu: {len(_inline_analyses)} plików. Sprawdź proponowane warstwy i zatwierdź ustawienia.")
            _inline_all_layers = sorted({str(x.get('Warstwa')) for a in _inline_analyses for x in a.get('warstwy',[]) if x.get('Warstwa')})
            _inline_route_default = [x for x in st.session_state.get('cable_inline_profile_route_suggestions',[]) if x in _inline_all_layers]
            _inline_device_default = [x for x in st.session_state.get('cable_inline_profile_device_suggestions',[]) if x in _inline_all_layers]
            _inline_routes = st.multiselect('Zatwierdź warstwy tras kablowych', _inline_all_layers, default=_inline_route_default, key='cable_inline_profile_routes_confirm')
            _inline_devices = st.multiselect('Zatwierdź warstwy urządzeń / punktów końcowych', _inline_all_layers, default=_inline_device_default, key='cable_inline_profile_devices_confirm')
            st.dataframe(pd.DataFrame([{'Plik':a.get('plik'), 'Format':a.get('format'), 'Rola rysunku':a.get('rola_rysunku'), 'Liczba warstw':len(a.get('warstwy',[])), 'Typy kabli':', '.join(a.get('wzorce',{}).get('typy_kabli',[])[:12])} for a in _inline_analyses]), use_container_width=True, hide_index=True)
            if st.button('✅ Zatwierdź profil i zastosuj w pomiarze tras', use_container_width=True, key='cable_inline_profile_apply'):
                _inline_unit = {'Automatycznie / wg pliku':'auto','mm':'mm','cm':'cm','m':'m'}[_inline_profile_units_label]
                _inline_profile = build_profile(_inline_profile_name.strip() or 'Nowy projekt', _inline_analyses, _inline_routes, _inline_devices, _inline_unit, float(_inline_profile_height), float(_inline_profile_reserve))
                st.session_state['thanos_project_profile'] = _inline_profile
                st.session_state['profile_project_name'] = _inline_profile['nazwa_projektu']
                st.session_state['profile_units'] = _inline_profile_units_label
                st.session_state['profile_height'] = float(_inline_profile_height)
                st.session_state['profile_reserve'] = float(_inline_profile_reserve)
                st.session_state['cable_room_height'] = float(_inline_profile_height)
                st.session_state['cable_reserve_pct'] = float(_inline_profile_reserve)
                st.success(f"Zastosowano profil „{_inline_profile['nazwa_projektu']}”. Warstwy tras: {len(_inline_routes)}; warstwy urządzeń: {len(_inline_devices)}. Profil jest aktywny poniżej w sekcji pomiaru tras.")
                st.rerun()
            _inline_profile_for_download = build_profile(_inline_profile_name.strip() or 'Nowy projekt', _inline_analyses, _inline_routes, _inline_devices, {'Automatycznie / wg pliku':'auto','mm':'mm','cm':'cm','m':'m'}[_inline_profile_units_label], float(_inline_profile_height), float(_inline_profile_reserve))
            st.download_button('⬇ Zapisz zatwierdzony wybór jako profil JSON', data=json.dumps(_inline_profile_for_download,ensure_ascii=False,indent=2).encode('utf-8'), file_name='profil_projektu.json', mime='application/json', use_container_width=True, key='cable_inline_profile_download')
        else:
            st.info('Wybierz co najmniej jeden plik PDF lub DXF, aby uruchomić analizę profilu.')

    source_files = list(staged) + list(uploaded)
    # De-duplicate handed-off and separately selected files by filename + content.
    unique_files = []
    seen_files = set()
    for item in source_files:
        try:
            sig = (getattr(item, 'name', ''), hashlib.sha1(item.getvalue()).hexdigest())
        except Exception:
            sig = (getattr(item, 'name', ''), id(item))
        if sig not in seen_files:
            seen_files.add(sig); unique_files.append(item)
    st.caption(f'Pliki do analizy: {len(unique_files)}. W przypadku DWG wymagana jest dostępna instalacja ODA File Converter.')
    if st.button('🔎 Rozpoznaj oznaczenia kabli', type='primary', use_container_width=True, key='cable_list_run'):
        try:
            with st.spinner('Analizuję schematy i wyszukuję oznaczenia kabli…'):
                _cable_df = extract_cable_candidates(unique_files, convert_dwg=auto_convert_dwg_to_dxf)
            st.session_state['cable_list_df'] = _cable_df
            st.session_state['cable_list_source_names'] = [getattr(f, 'name', 'plik') for f in unique_files]
            st.session_state['cable_list_errors'] = _cable_df.attrs.get('errors', [])
            st.session_state.pop('cable_list_edited_df', None)
        except Exception as _cable_exc:
            st.error(f'Nie udało się przeanalizować schematów: {type(_cable_exc).__name__}: {_cable_exc}')
    for _cable_error in st.session_state.get('cable_list_errors', []):
        st.warning(_cable_error)
    _cable_df = st.session_state.get('cable_list_edited_df', st.session_state.get('cable_list_df', pd.DataFrame(columns=CABLE_COLUMNS)))
    if isinstance(_cable_df, pd.DataFrame):
        st.markdown('#### Dodaj pozycję ręcznie')
        st.caption('Możesz wpisać dane bezpośrednio w tabeli albo dodać nowy wiersz formularzem. Pola źródła, odbiornika i oznaczenia obwodu są edytowalne.')
        with st.form('cable_list_manual_row_form', clear_on_submit=True):
            _manual_cols = st.columns(3)
            with _manual_cols[0]:
                _manual_source = st.text_input('Rozdzielnica źródłowa', key='cable_manual_source')
            with _manual_cols[1]:
                _manual_target = st.text_input('Odbiornik / rozdzielnica docelowa', key='cable_manual_target')
            with _manual_cols[2]:
                _manual_circuit = st.text_input('Oznaczenie obwodu', key='cable_manual_circuit')
            _manual_cable = st.text_input('Typ kabla (opcjonalnie)', key='cable_manual_type')
            _manual_add = st.form_submit_button('＋ Dodaj pozycję do listy', use_container_width=True)
        if _manual_add:
            _new_row = {col: '' for col in CABLE_COLUMNS}
            _new_row.update({
                'Rozdzielnica źródłowa': _manual_source.strip(),
                'Odbiornik / rozdzielnica docelowa': _manual_target.strip(),
                'Oznaczenie obwodu': _manual_circuit.strip(),
                'Typ kabla': _manual_cable.strip(),
                'Liczba równoległych kabli': 1,
                'Długość [m]': None,
                'Źródło': 'Wpis ręczny',
                'Strona / warstwa': '',
                'Status weryfikacji': 'Do weryfikacji',
                'Uwagi': 'Pozycja dodana ręcznie',
            })
            _base_df = st.session_state.get('cable_list_edited_df', st.session_state.get('cable_list_df', pd.DataFrame(columns=CABLE_COLUMNS)))
            _base_df = _base_df.reindex(columns=CABLE_COLUMNS)
            st.session_state['cable_list_edited_df'] = pd.concat([_base_df, pd.DataFrame([_new_row], columns=CABLE_COLUMNS)], ignore_index=True)
            st.session_state.pop('cable_list_editor', None)
            st.rerun()
        _cable_df = st.session_state.get('cable_list_edited_df', st.session_state.get('cable_list_df', pd.DataFrame(columns=CABLE_COLUMNS)))
        st.markdown('#### Automatyczny pomiar tras z rzutu DXF')
        st.caption('Wczytaj DXF z warstwami tras albo CSV diagnostyczny geometrii Cablofil. Graf łączy wyłącznie końce odcinków w tej samej klasie sieci; samo przecięcie linii nie tworzy połączenia. Sieci elektryczne i teletechniczne są rozdzielane. Gdy brak warstw tras, można dodatkowo uzyskać wyraźnie oznaczony szacunek po suficie między jednoznacznymi etykietami na tym samym rzucie.')
        _active_profile = st.session_state.get('thanos_project_profile', {}) or {}
        _profile_route_layers = [str(x) for x in _active_profile.get('warstwy_tras_kablowych', []) if str(x).strip()]
        _profile_device_layers = [str(x) for x in _active_profile.get('warstwy_urządzeń', []) if str(x).strip()]
        _profile_unit = str(_active_profile.get('jednostki_CAD', 'auto') or 'auto').casefold()
        _profile_unit = _profile_unit if _profile_unit in {'auto','mm','cm','m'} else 'auto'
        if _active_profile:
            st.info(f"Aktywny profil: {_active_profile.get('nazwa_projektu','bez nazwy')} · warstwy tras: {', '.join(_profile_route_layers) or 'brak — zostaną użyte warstwy domyślne'} · jednostki: {_profile_unit}")
        _route_uploads = st.file_uploader('Rzuty tras kablowych (DXF) i/lub diagnostyka osi Cablofil (CSV) — można wybrać kilka plików', type=['dxf','csv'], accept_multiple_files=True, key='cable_route_dxf_uploads') or []
        if _route_uploads:
            st.caption('Wgraj razem rzuty wszystkich kondygnacji tego samego projektu. Schematy RG/TP1 pomagają ustalić końce kabla, ale nie są mierzone jako fizyczne trasy.')
        if st.button('📐 Zmierz trasy ze wszystkich plików', key='cable_route_measure', use_container_width=True, disabled=not _route_uploads):
            try:
                _route_frames = []
                _route_errors = []
                with st.spinner(f'Mierzę geometrię w {len(_route_uploads)} plikach…'):
                    for _route_file in _route_uploads:
                        try:
                            if Path(_route_file.name).suffix.lower() == '.csv':
                                _one_routes = measure_cablofil_csv_routes(_route_file)
                            else:
                                _one_routes = measure_dxf_cable_routes(
                                    _route_file,
                                    route_layers=_profile_route_layers or None,
                                    unit_override=_profile_unit,
                                )
                            if not _one_routes.empty:
                                _one_routes = _one_routes.copy()
                                _one_routes['ID trasy'] = [f'{Path(_route_file.name).stem[:18]}:{_rid}' for _rid in _one_routes['ID trasy'].astype(str)]
                                _one_routes['Źródło'] = _route_file.name
                                _route_frames.append(_one_routes)
                        except Exception as _one_exc:
                            _route_errors.append(f'{_route_file.name}: {type(_one_exc).__name__}: {_one_exc}')
                _route_df = pd.concat(_route_frames, ignore_index=True) if _route_frames else pd.DataFrame(columns=['ID trasy','Długość trasy [m]','Liczba odcinków','Typ sieci','Warstwy','Etykiety w pobliżu','Etykieta początku','Etykieta końca','Ścieżki między etykietami [JSON]','Źródło'])
                st.session_state['cable_measured_routes'] = _route_df
                st.session_state['cable_measured_routes_source'] = ', '.join(f.name for f in _route_uploads)
                st.session_state['cable_route_measure_errors'] = _route_errors
                if _route_errors:
                    for _msg in _route_errors: st.warning(_msg)
                if _route_df.empty:
                    st.warning('Nie znaleziono geometrii tras w wybranych plikach. Sprawdź warstwy i jednostki DXF.')
                else:
                    st.success(f'Zidentyfikowano {len(_route_df)} komponentów tras z {len(_route_uploads)} plików. Wyniki pozostają diagnostyczne; długości kabli wymagają przypisania przebiegu między źródłem i odbiornikiem.')
            except Exception as _route_exc:
                st.error(f'Nie udało się zmierzyć tras: {type(_route_exc).__name__}: {_route_exc}')
        if st.button('📏 Oszacuj przebieg tam, gdzie brak zdefiniowanych tras', key='cable_route_estimate_missing', use_container_width=True, disabled=not _route_uploads):
            try:
                _estimate_df = estimate_cable_paths_without_route_layers(
                    [f for f in _route_uploads if Path(f.name).suffix.lower() == '.dxf'],
                    _cable_df,
                    unit_override=_profile_unit,
                )
                st.session_state['cable_route_estimates'] = _estimate_df
                if _estimate_df.empty:
                    st.warning('Nie utworzono szacunków. Potrzebne są dokładnie po jednej etykiecie źródła i odbiornika w tym samym DXF oraz znane jednostki rysunku.')
                else:
                    st.warning(f'Utworzono {len(_estimate_df)} szacunków ortogonalnych. Nie są to pomiary rzeczywiste i wymagają sprawdzenia na rzucie.')
            except Exception as _estimate_exc:
                st.error(f'Nie udało się oszacować brakujących tras: {type(_estimate_exc).__name__}: {_estimate_exc}')
        _route_estimates = st.session_state.get('cable_route_estimates', pd.DataFrame())
        if isinstance(_route_estimates, pd.DataFrame) and not _route_estimates.empty:
            st.markdown('#### Szacunki dla pozycji bez zdefiniowanej trasy')
            st.dataframe(_route_estimates, use_container_width=True, hide_index=True)
            _estimate_options = list(range(len(_route_estimates)))
            _estimate_choice = st.selectbox('Szacunek do zastosowania', options=_estimate_options,
                format_func=lambda i: f"{_route_estimates.iloc[i]['Oznaczenie obwodu'] or 'bez oznaczenia'} — {_route_estimates.iloc[i]['Długość trasy [m]']:g} m — {_route_estimates.iloc[i]['Plik źródłowy']}",
                key='cable_route_estimate_choice')
            if st.button('Zastosuj wybrany szacunek do listy kablowej', key='cable_route_estimate_apply', use_container_width=True):
                _est = _route_estimates.iloc[int(_estimate_choice)]
                _target = st.session_state.get('cable_list_edited_df', _cable_df).copy()
                _idx = int(_est['Indeks pozycji'])
                if 0 <= _idx < len(_target):
                    _target.loc[_target.index[_idx], 'Długość trasy [m]'] = float(_est['Długość trasy [m]'])
                    _target.loc[_target.index[_idx], 'Status weryfikacji'] = 'Wymaga wyjaśnienia'
                    _target.loc[_target.index[_idx], 'Uwagi'] = (str(_target.iloc[_idx].get('Uwagi') or '') + f" | {_est['Uwagi']} Źródło: {_est['Plik źródłowy']}.").strip(' |')
                    st.session_state['cable_list_edited_df'] = _target
                    st.success('Zastosowano szacunek jako długość trasy z oznaczeniem wymagającym weryfikacji.')
                    st.rerun()
        _measured_routes = st.session_state.get('cable_measured_routes', pd.DataFrame())
        _measured_routes = st.session_state.get('cable_measured_routes', pd.DataFrame())
        if isinstance(_measured_routes, pd.DataFrame) and not _measured_routes.empty:
            st.dataframe(_measured_routes, use_container_width=True, hide_index=True)
            _auto_matches = suggest_cable_route_matches(_cable_df, _measured_routes)
            if st.button('🔗 Dopasuj automatycznie jednoznaczne trasy', key='cable_route_auto_match', use_container_width=True, disabled=_auto_matches.empty):
                _auto_target = st.session_state.get('cable_list_edited_df', _cable_df).copy()
                _assigned = []
                for _, _match in _auto_matches.iterrows():
                    _idx = int(_match['Indeks pozycji'])
                    _auto_target.loc[_auto_target.index[_idx], 'Długość trasy [m]'] = float(_match['Długość trasy [m]'])
                    _auto_target.loc[_auto_target.index[_idx], 'Uwagi'] = (str(_auto_target.iloc[_idx].get('Uwagi') or '') + f" | Automatyczne dopasowanie dokładnej etykiety: {_match['Etykieta dopasowania']} → {_match['ID trasy']}.").strip(' |')
                    _auto_target.loc[_auto_target.index[_idx], 'Status weryfikacji'] = 'Do weryfikacji'
                    _assigned.append(str(_match['ID trasy']))
                st.session_state['cable_list_edited_df'] = _auto_target
                st.success(f"Przypisano {len(_assigned)} jednoznacznych tras na podstawie dokładnych etykiet. Wyniki pozostają do weryfikacji.")
                st.rerun()
            if not _auto_matches.empty:
                st.caption('Kandydaci do automatycznego przypisania — wymagane dokładne dopasowanie etykiety, bez zgadywania na podstawie bliskości.')
                st.dataframe(_auto_matches, use_container_width=True, hide_index=True)
            else:
                _route_label_values = []
                for _, _r in _measured_routes.iterrows():
                    _route_label_values.extend([x.strip() for x in str(_r.get('Etykiety w pobliżu', '') or '').split(',') if x.strip()])
                _route_label_values = list(dict.fromkeys(_route_label_values))
                _circuit_values = []
                for _, _c in _cable_df.iterrows():
                    for _col in ('Oznaczenie obwodu', 'Odbiornik / rozdzielnica docelowa'):
                        _v = _c.get(_col, '')
                        if pd.notna(_v) and str(_v).strip() and str(_v).strip().casefold() not in {'nan', 'none'}:
                            _circuit_values.append(str(_v).strip())
                st.warning('Brak jednoznacznych ścieżek: Thanos szuka teraz najkrótszej drogi w grafie geometrii między etykietą rozdzielnicy źródłowej i odbiornikiem, które muszą znajdować się blisko węzłów trasy. Jeśli etykiety nie leżą na rzucie tras albo są tylko na schemacie RG/TP1, automatyczne przypisanie wymaga jeszcze powiązania schematu z rzutem.')
                st.caption(f"Kontrola danych: {len(_circuit_values)} oznaczeń obwodów/odbiorników w liście; {len(_route_label_values)} różnych etykiet przy trasach. Thanos szuka etykiet do 1,8 m od węzłów geometrii. Jeśli schemat podaje źródło/odbiornik, ale rzut nie łączy ich z tą samą ciągłą trasą, dopasowanie pozostaje niepotwierdzone. Przykładowe etykiety tras: {', '.join(_route_label_values[:12]) or 'brak etykiet'}.")
            _route_options = ['— wybierz trasę —'] + [f"{r['ID trasy']} | {float(r['Długość trasy [m]']):g} m | {str(r['Etykiety w pobliżu'])[:80]}" for _, r in _measured_routes.iterrows()]
            _map1, _map2 = st.columns([2, 3])
            with _map1:
                _cable_row_idx = st.selectbox('Pozycja listy kablowej', options=list(range(len(_cable_df))), format_func=lambda i: f"{i+1}. {('bez oznaczenia' if pd.isna(_cable_df.iloc[i].get('Oznaczenie obwodu')) or str(_cable_df.iloc[i].get('Oznaczenie obwodu')).strip().casefold() in {'', 'nan', 'none'} else str(_cable_df.iloc[i].get('Oznaczenie obwodu')).strip())} — {('bez odbiornika' if pd.isna(_cable_df.iloc[i].get('Odbiornik / rozdzielnica docelowa')) or str(_cable_df.iloc[i].get('Odbiornik / rozdzielnica docelowa')).strip().casefold() in {'', 'nan', 'none'} else str(_cable_df.iloc[i].get('Odbiornik / rozdzielnica docelowa')).strip())}", key='cable_route_target_row') if len(_cable_df) else None
            with _map2:
                _chosen_route = st.selectbox('Zmierzona trasa', options=list(range(len(_route_options))), format_func=lambda i: _route_options[i], key='cable_route_selected_component')
            if st.button('Przypisz długość trasy do wybranej pozycji', key='cable_route_apply', use_container_width=True, disabled=(_cable_row_idx is None or _chosen_route == 0)):
                _target_df = st.session_state.get('cable_list_edited_df', _cable_df).copy()
                _selected = _measured_routes.iloc[_chosen_route-1]
                _target_df.loc[_target_df.index[_cable_row_idx], 'Długość trasy [m]'] = float(_selected['Długość trasy [m]'])
                _target_df.loc[_target_df.index[_cable_row_idx], 'Uwagi'] = (str(_target_df.iloc[_cable_row_idx].get('Uwagi') or '') + f" | Przypisana geometria {_selected['ID trasy']} z {_selected['Źródło']}.").strip(' |')
                st.session_state['cable_list_edited_df'] = _target_df
                st.success(f"Przypisano {float(_selected['Długość trasy [m]']):g} m do wybranej pozycji.")
                st.rerun()
        st.markdown('#### Parametry obliczania długości kabli')
        st.caption('Wpisz wysokości i zapas. Długość trasy oraz odcinek poza trasą można uzupełnić po pomiarze lub przypisaniu geometrii z rzutu. Thanos nie przypisuje automatycznie niejednoznacznej trasy do obwodu.')
        _p1, _p2, _p3, _p4 = st.columns(4)
        with _p1:
            _room_h = st.number_input('Wysokość pomieszczenia [m]', min_value=0.0, max_value=20.0, value=float(st.session_state.get('cable_room_height', 3.0)), step=0.1, key='cable_room_height')
        with _p2:
            _source_h = st.number_input('Wysokość wyjścia z rozdzielnicy [m]', min_value=0.0, max_value=20.0, value=float(st.session_state.get('cable_source_height', 0.3)), step=0.1, key='cable_source_height')
        with _p3:
            _device_h = st.number_input('Wysokość montażu odbiornika [m]', min_value=0.0, max_value=20.0, value=float(st.session_state.get('cable_device_height', 2.0)), step=0.1, key='cable_device_height')
        with _p4:
            _reserve_pct = st.number_input('Zapas kabla [%]', min_value=0.0, max_value=100.0, value=float(st.session_state.get('cable_reserve_pct', 10.0)), step=1.0, key='cable_reserve_pct')
        st.caption('Parametry domyślne: pomieszczenie 3,0 m, wyjście z rozdzielnicy 0,3 m, odbiornik 2,0 m, zapas 10%. Wzór: (długość trasy + odcinek poza trasą + max(0, wysokość pomieszczenia − wysokość wyjścia) + max(0, wysokość pomieszczenia − wysokość odbiornika)) × (1 + zapas%). Wspólna trasa jest liczona raz; ilość materiału wylicza się osobno według liczby kabli równoległych.')
        st.markdown('#### Lista kablowa')
        _primary_cols = [c for c in ['Oznaczenie obwodu', 'Rozdzielnica źródłowa', 'Odbiornik / rozdzielnica docelowa', 'Typ kabla', 'Liczba równoległych kabli', 'Długość [m]', 'Status weryfikacji'] if c in _cable_df.columns]
        st.dataframe(_cable_df.reindex(columns=_primary_cols), use_container_width=True, hide_index=True)
        with st.expander('✏️ Edycja szczegółowa i pola techniczne', expanded=False):
            st.caption('Tutaj możesz edytować wszystkie pola, w tym długości tras, odcinki poza trasą, źródło, warstwę i uwagi. Eksport zachowuje pełny zestaw danych.')
            _edited_cables = st.data_editor(_cable_df.reindex(columns=CABLE_COLUMNS), key='cable_list_editor', use_container_width=True, hide_index=True, num_rows='dynamic',
                column_config={
                    'Rozdzielnica źródłowa': st.column_config.TextColumn('Rozdzielnica źródłowa', help='Wpisz nazwę rozdzielnicy źródłowej.'),
                    'Odbiornik / rozdzielnica docelowa': st.column_config.TextColumn('Odbiornik / rozdzielnica docelowa', help='Wpisz odbiornik lub rozdzielnicę docelową.'),
                    'Oznaczenie obwodu': st.column_config.TextColumn('Oznaczenie obwodu', help='Wpisz oznaczenie obwodu, np. WL-01.'),
                    'Liczba równoległych kabli': st.column_config.NumberColumn(min_value=1, step=1),
                    'Długość trasy [m]': st.column_config.NumberColumn('Długość trasy [m]', min_value=0.0, step=0.5, help='Długość zmierzona po trasie kablowej na rzucie.'),
                    'Odcinek poza trasą [m]': st.column_config.NumberColumn('Odcinek poza trasą [m]', min_value=0.0, step=0.5, help='Odcinek prowadzony sufitem poza korytem/trasa.'),
                    'Długość [m]': st.column_config.NumberColumn(min_value=0, step=0.1),
                    'Status weryfikacji': st.column_config.SelectboxColumn(options=['Do weryfikacji','Potwierdzono na schemacie','Wymaga wyjaśnienia']),
                })
        st.session_state['cable_list_edited_df'] = _edited_cables
        if st.button('Oblicz długości z parametrów', key='cable_calc_lengths', use_container_width=True):
            _calc = _edited_cables.copy()
            _route = pd.to_numeric(_calc.get('Długość trasy [m]', pd.Series(index=_calc.index, dtype=float)), errors='coerce')
            _outside = pd.to_numeric(_calc.get('Odcinek poza trasą [m]', pd.Series(index=_calc.index, dtype=float)), errors='coerce').fillna(0.0)
            _source_drop = max(0.0, float(_room_h) - float(_source_h))
            _device_drop = max(0.0, float(_room_h) - float(_device_h))
            _base = _route + _outside + _source_drop + _device_drop
            _calc['Długość [m]'] = (_base * (1.0 + float(_reserve_pct) / 100.0)).where(_route.notna(), pd.to_numeric(_calc.get('Długość [m]', pd.Series(index=_calc.index, dtype=float)), errors='coerce'))
            _calc['Uwagi'] = _calc.apply(lambda r: (str(r.get('Uwagi') or '') + f' | Długość obliczona: trasa + odcinek poza trasą + piony; wysokość pom.: {_room_h:g} m; zapas: {_reserve_pct:g}%.').strip(' |'), axis=1)
            st.session_state['cable_list_edited_df'] = _calc
            st.success('Obliczono długości dla pozycji z uzupełnioną długością trasy. Pozostałe pozycje nie zostały zmienione.')
            st.rerun()
        _cc1, _cc2, _cc3 = st.columns(3)
        with _cc1:
            st.metric('Pozycji', len(_edited_cables))
        with _cc2:
            st.metric('Typów kabli', int(_edited_cables['Typ kabla'].fillna('').astype(str).nunique()) if not _edited_cables.empty else 0)
        with _cc3:
            _lengths = pd.to_numeric(_edited_cables['Długość [m]'], errors='coerce')
            st.metric('Suma wpisanych długości', f'{_lengths.sum():g} m' if _lengths.notna().any() else 'Nie uzupełniono')
        _xlsx_cables = export_cable_list_xlsx(_edited_cables)
        st.download_button('⬇ Pobierz listę kablową XLSX', data=_xlsx_cables, file_name=_xlsx_filename('Lista_kablowa'), mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', use_container_width=True, key='download_cable_list_xlsx')
        if st.button('Wyczyść listę kablową', key='clear_cable_list'):
            st.session_state.pop('cable_list_df', None); st.session_state.pop('cable_list_edited_df', None); st.session_state.pop('cable_list_errors', None); st.session_state.pop('cable_route_estimates', None)
            st.rerun()
    st.markdown('<div class="th-footer"><span>Thanos v' + APP_VERSION + '</span><span>Autor aplikacji: Paweł Świerk</span></div>', unsafe_allow_html=True)
    st.stop()

files = st.session_state.get('main_files', [])
st.session_state.project_name = st.session_state.get('project_name', 'Zestawienie elementów')
for key, default in [('odg_geom', pd.DataFrame()), ('odg_blocks', pd.DataFrame()), ('measurements', pd.DataFrame())]:
    st.session_state.setdefault(key, default)
st.session_state.setdefault('odg_error', '')

# --- Główny wybór plików i rozdzielone opcje PDF / DWG-DXF ---
_is_cad = st.session_state.get('ui_page') == 'cad'

if _is_cad:
    st.markdown('<div class="th-section-title">▱ &nbsp; 1. Wybierz pliki DWG / DXF</div>', unsafe_allow_html=True)
    cad_files = st.file_uploader('Wybierz pliki DWG / DXF', type=['dwg','dxf'], accept_multiple_files=True, key='cad_files', label_visibility='collapsed') or []
    st.markdown('<div class="th-help">Obsługiwane: DWG i DXF. Pliki DWG są automatycznie konwertowane do DXF, jeśli dostępny jest ODA File Converter.</div>', unsafe_allow_html=True)
    files = cad_files
    if st.button('⌁ Przekaż wybrane pliki do Listy kablowej', key='handoff_cad_to_cable', use_container_width=True, disabled=not bool(cad_files)):
        st.session_state['cable_handoff_files'] = list(cad_files)
        st.session_state['ui_page'] = 'cables'
        st.rerun()

    with st.expander('⚙  2. Opcje analizy DWG / DXF', expanded=False):
        c1,c2=st.columns(2)
        with c1:
            st.checkbox('Automatyczna konwersja DWG → DXF', value=st.session_state.get('auto_dwg_to_dxf', True), key='cad_auto_convert')
            st.checkbox('Automatyczne wykrywanie legendy', value=True, key='cad_auto_legend')
            st.checkbox('Wyklucz legendę ze zliczania', value=st.session_state.get('exclude_legend', False), key='cad_exclude_legend')
        with c2:
            st.checkbox('Rozpoznawanie bloków CAD', value=True, key='cad_blocks')
            st.checkbox('Grupuj obiekty graficzne', value=True, key='cad_group_graphics')
            st.selectbox('Metoda zliczania', ['Wg bloków CAD','Wg grafik na legendzie'], key='cad_count_mode')
    st.session_state.auto_dwg_to_dxf = st.session_state.get('cad_auto_convert', True)
    st.session_state.exclude_legend = st.session_state.get('cad_exclude_legend', False)
    st.session_state.dxf_count_mode = st.session_state.get('cad_count_mode', 'Wg bloków CAD')


else:
    st.markdown('<div class="th-section-title">▱ &nbsp; 1. Wybierz pliki PDF do analizy</div>', unsafe_allow_html=True)
    pdf_files = st.file_uploader('Wybierz pliki PDF', type=['pdf'], accept_multiple_files=True, key='main_files', label_visibility='collapsed') or []
    st.markdown('<div class="th-help">Obsługiwane: PDF wektorowe i skany. Analiza PDF obejmuje wszystkie strony.</div>', unsafe_allow_html=True)
    files = pdf_files
    if st.button('⌁ Przekaż wybrane pliki do Listy kablowej', key='handoff_pdf_to_cable', use_container_width=True, disabled=not bool(pdf_files)):
        st.session_state['cable_handoff_files'] = list(pdf_files)
        st.session_state['ui_page'] = 'cables'
        st.rerun()

    with st.expander('⚙  2. Opcje analizy PDF', expanded=False):
        o1,o2=st.columns(2)
        with o1:
            st.checkbox('Automatyczne wykrywanie legendy',value=True,key='pdf_auto_legend')
            st.checkbox('Rozpoznawanie symboli graficznych',value=True,key='pdf_graphic_match')
            st.checkbox('Analiza tekstu na rysunku',value=True,key='pdf_text_analysis')
            st.checkbox('Wyklucz legendę ze zliczania',value=True,key='pdf_legend_only')
            st.checkbox('Grupuj obiekty graficzne',value=True,key='pdf_group_graphics')
        with o2:
            st.checkbox('Ignoruj tabelę tytułową',value=True,key='pdf_ignore_title')
            st.checkbox('Pomijaj oznaczenia obwodów',value=True,key='pdf_skip_circuit')
            st.checkbox('Wysoka dokładność (wolniej)',value=False,key='pdf_high_accuracy')
            st.checkbox('Zapis podglądu analizy',value=False,key='pdf_save_preview')
    st.session_state.pdf_exclude_legend = st.session_state.get('pdf_legend_only', True)

# Detect a genuinely new document set before rendering the common workflow.
# This runs after the uploader has produced its current files, so changing the
# selection starts a clean analysis project while normal Streamlit reruns do not.
_current_files_fingerprint = _uploaded_files_fingerprint(files, page='cad' if _is_cad else 'pdf')
_previous_files_fingerprint = st.session_state.get('_loaded_files_fingerprint')

# Zmiana motywu jasny/ciemny wykonuje rerun Streamlit. Nie jest to zmiana
# dokumentacji i nie może uruchamiać _reset_for_new_document_set().
_skip_document_reset = bool(st.session_state.pop('_skip_document_reset_once', False))
if _previous_files_fingerprint != _current_files_fingerprint:
    if not _skip_document_reset:
        _reset_for_new_document_set()
    st.session_state['_loaded_files_fingerprint'] = _current_files_fingerprint

# Common workflow below intentionally uses `files` for both modes.
a1,a2=st.columns([2.2,1.0])
with a1:
    run_analysis=st.button('▶  Rozpocznij analizę',type='primary',use_container_width=True,key='run_analysis_btn')
    if run_analysis:
        st.session_state.analysis_requested=True
        st.session_state.analysis_last_error=''
with a2:
    top_xlsx=st.session_state.get('xlsx_bytes')
    st.download_button('⬇  Pobierz Excel',data=top_xlsx if top_xlsx else b'',file_name=_xlsx_filename(st.session_state.project_name),mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',use_container_width=True,disabled=not bool(top_xlsx),key='download_xlsx_top')
    if top_xlsx:
        # Sygnał gotowego XLSX: styl wstawiany po wyrenderowaniu przycisku.
        st.markdown("""
        <style>
        @keyframes thanos-xlsx-ready-pulse {
          0%,100% { box-shadow: 0 0 0 0 rgba(22,163,74,.10); }
          50% { box-shadow: 0 0 0 6px rgba(22,163,74,.32); }
        }
        div[data-testid="stDownloadButton"] button {
          animation: thanos-xlsx-ready-pulse 1.25s ease-in-out infinite !important;
          background:#16A34A !important; border-color:#16A34A !important; color:#FFFFFF !important;
        }
        div[data-testid="stDownloadButton"] button:hover {
          background:#15803D !important; border-color:#15803D !important;
        }
        </style>
        """, unsafe_allow_html=True)
current_stage=1 if files else 0
if st.session_state.get('analysis_running'): current_stage=3
if st.session_state.get('xlsx_bytes'): current_stage=5
labels=['Wczytywanie','Wykrywanie legendy','Analiza symboli','Zliczanie','Zapis wyniku']
steps=''.join([f'<div class="th-step {"active" if i==current_stage else "done" if i<current_stage else ""}"><div class="th-step-dot">{i}</div><div>{lab}</div></div>' for i,lab in enumerate(labels,1)])
st.markdown('<div class="th-progress"><b>Postęp analizy</b><div class="th-progress-steps">'+steps+'</div></div>',unsafe_allow_html=True)
if st.session_state.get('analysis_last_error'): st.error(st.session_state.analysis_last_error)

# Własna nawigacja modułów oparta o st.button zamiast st.tabs().
_module_labels = (
    ['🔎 Analiza DWG/DXF', '📏 Pomiar tras', '⚡ Odgrom/uziom', '📋 Excel → ATH']
    if _is_cad else
    ['🔎 Analiza PDF', '📏 Pomiar tras', '⚡ Odgrom/uziom', '🖼️ Podgląd PDF', '📋 Excel → ATH']
)
# The PDF and CAD modes have different module lists. Never reuse the same
# numeric tab index across modes: index 3 means PDF preview in PDF mode but
# Excel → ATH in CAD mode. Reset navigation on mode change and namespace the
# button keys by context so Streamlit cannot reuse stale widget state.
_module_context = 'cad' if _is_cad else 'pdf'
_resolved_module, _resolved_context = resolve_active_module(
    _module_context,
    st.session_state.get('_active_module_context'),
    st.session_state.get('active_module'),
    len(_module_labels),
)
st.session_state.active_module = _resolved_module
st.session_state['_active_module_context'] = _resolved_context

_dark_nav = st.session_state.get('theme_mode','dark') == 'dark'
_nav_border = '#374151' if _dark_nav else '#D7DEE7'
_nav_bg = '#151D2D' if _dark_nav else '#FFFFFF'
_nav_item = '#1F2937' if _dark_nav else '#F8FAFC'
_nav_text = '#E5E7EB' if _dark_nav else '#172033'
_nav_hover = '#24324A' if _dark_nav else '#FDECEC'
_nav_active = '#FF4D4D' if _dark_nav else '#E53935'
_nav_active_bg = '#2A2028' if _dark_nav else '#FFF1F1'
_nav_keys = ','.join(f'.st-key-module_nav_{_module_context}_{i}' for i in range(len(_module_labels)))
_nav_active_key = f'.st-key-module_nav_{_module_context}_{st.session_state.active_module}'
st.markdown(f'''<style>
{_nav_keys} {{ margin:0!important; padding:0!important; }}
{_nav_keys} button {{ width:100%!important; min-height:48px!important; padding:8px 10px!important; border-radius:9px!important; border:1px solid {_nav_border}!important; background:{_nav_item}!important; color:{_nav_text}!important; font-weight:650!important; white-space:nowrap!important; box-shadow:0 1px 2px rgba(0,0,0,.08)!important; transition:all .15s ease!important; }}
{_nav_keys} button:hover {{ background:{_nav_hover}!important; border-color:{_nav_active}!important; color:{_nav_active}!important; transform:translateY(-1px)!important; }}
{_nav_active_key} button {{ background:{_nav_active_bg}!important; border-color:{_nav_active}!important; color:{_nav_active}!important; box-shadow:inset 0 -3px 0 { _nav_active },0 2px 6px rgba(0,0,0,.10)!important; }}
@media (max-width:1050px) {{ {_nav_keys} button {{ font-size:13px!important; }} }}
</style>''', unsafe_allow_html=True)
_nav_cols = st.columns(len(_module_labels), gap='small')
for _i, (_col, _label) in enumerate(zip(_nav_cols, _module_labels)):
    with _col:
        if st.button(_label, key=f'module_nav_{_module_context}_{_i}', type='secondary', use_container_width=True):
            st.session_state.active_module = _i
            st.rerun()
active_module = int(st.session_state.active_module)

if not files and active_module != (3 if _is_cad else 4):
    st.markdown('<div class="th-empty">Wczytaj dokumentację, aby rozpocząć analizę.</div>', unsafe_allow_html=True)
    st.info(f'Wczytaj dokumentację, aby aktywować zawartość modułu: {_module_labels[active_module]}.')

else:
    nfiles = len(files)
    nrows = int(st.session_state.results['Ilość'].sum()) if not st.session_state.results.empty else 0
    ntypes = int(st.session_state.results['Typ elementu'].nunique()) if not st.session_state.results.empty else 0
    c0, c1, c2, c3 = st.columns([1.6,1,1,1])
    with c0:
        st.markdown(f'<div class="th-card"><div class="th-card-title">Dokumentacja</div><div class="th-card-value">{nfiles}</div><div style="color:#7f9bad;font-size:12px">plików w projekcie</div></div>', unsafe_allow_html=True)
    with c1:
        st.markdown(f'<div class="th-card"><div class="th-card-title">Elementy</div><div class="th-card-value">{nrows}</div><div style="color:#7f9bad;font-size:12px">wykrytych</div></div>', unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="th-card"><div class="th-card-title">Typy</div><div class="th-card-value">{ntypes}</div><div style="color:#7f9bad;font-size:12px">rodzajów elementów</div></div>', unsafe_allow_html=True)
    with c3:
        st.markdown(f'<div class="th-card"><div class="th-card-title">Eksport</div><div class="th-card-value">XLSX</div><div style="color:#7f9bad;font-size:12px">gotowy format kosztorysowy</div></div>', unsafe_allow_html=True)
    st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)

    if active_module == 0:
        if _is_cad:
            st.caption('Zakres analizy CAD: wyłącznie Model Space. Encje z Layout / Paper Space są pomijane.')
            _scope_files, _scope_conv_msgs = get_dxf_files_with_auto_conversion(files, auto=st.session_state.get('auto_dwg_to_dxf', True))
            if _scope_files:
                _scope_selected = st.selectbox('Plik do kontroli zakresu (Analiza DWG/DXF)', _scope_files, format_func=lambda f: f.name, key='analysis_scope_file_26375')
                _render_model_layout_scope_audit(_scope_selected, 'analysis26375')
            else:
                st.caption('Kontrola Model / Layout będzie dostępna po wczytaniu pliku DXF lub konwersji DWG.')
        st.markdown('<div class="th-log-title">Dziennik zdarzeń</div>', unsafe_allow_html=True)
        _events = st.session_state.get('analysis_events', []) or []
        if _events:
            for _ev in _events[-100:]:
                _lvl = _ev.get('level', 'info') if isinstance(_ev, dict) else 'info'
                _msg = _ev.get('message', '') if isinstance(_ev, dict) else str(_ev)
                if _lvl == 'error': st.error(_msg)
                elif _lvl == 'warning': st.warning(_msg)
                elif _lvl == 'success': st.success(_msg)
                else: st.info(_msg)
        else:
            st.caption('Brak zdarzeń. Uruchom analizę, aby tutaj pojawił się dziennik przebiegu.')
        if st.session_state.get('analysis_requested', False) and not st.session_state.get('analysis_running', False):
            st.session_state.analysis_requested = False
            st.session_state.analysis_running = True
            st.session_state.analysis_last_error = ''
            st.session_state.analysis_events = []
            rows, pdf_pages, statuses = [], {}, {}
            errors = []
            def _event(message, level='info'):
                st.session_state.analysis_events.append({'level': level, 'message': str(message)})
            try:
                with st.status('Analiza dokumentacji...', expanded=True) as analysis_status:
                    bar = st.progress(0)
                    total_files = len(files)
                    _analysis_started = time.perf_counter()
                    _file_timings = []
                    if total_files == 0:
                        raise RuntimeError('Nie wybrano żadnych plików do analizy.')

                    for i, f in enumerate(files):
                        _file_started = time.perf_counter()
                        ext = Path(f.name).suffix.lower()
                        data = f.getvalue()
                        _event(f'Analizuję: {f.name} ({ext.upper()})', 'info'); analysis_status.write(f'Analizuję: {f.name} ({ext.upper()})')
                        try:
                            if ext == '.pdf':
                                r, pages, info = analyze_pdf(f.name, data, st.session_state.patterns, st.session_state.analysis_mode, st.session_state.ocr_lang, st.session_state.get('pdf_exclude_legend', True), st.session_state.get('pdf_auto_legend', True), st.session_state.get('pdf_graphic_match', True), st.session_state.get('pdf_text_analysis', True), st.session_state.get('pdf_group_graphics', True), st.session_state.get('pdf_ignore_title', True), st.session_state.get('pdf_skip_circuit', True), st.session_state.get('pdf_high_accuracy', False), st.session_state.get('pdf_save_preview', False))
                                rows.extend(r); pdf_pages[f.name] = pages; statuses[f.name] = info
                                _event(f'PDF: {f.name} — znaleziono {len(r)} pozycji wynikowych.', 'success'); analysis_status.write(f'  PDF: znaleziono {len(r)} pozycji wynikowych.')
                            elif ext == '.dxf':
                                dxf_rows, dxf_meta = analyze_dxf(f.name, data, st.session_state.patterns, st.session_state.exclude_legend, st.session_state.dxf_count_mode)
                                rows.extend(dxf_rows)
                                _event(f'DXF: {f.name} — znaleziono {len(dxf_rows)} pozycji wynikowych.', 'success'); analysis_status.write(f'  DXF: znaleziono {len(dxf_rows)} pozycji wynikowych.')
                                if not dxf_meta.empty:
                                    for _, m in dxf_meta.iterrows():
                                        statuses.setdefault(f.name, []).append(f'Layout {m["layout"]}: wykluczono bloki z legendy')
                            elif ext == '.dwg':
                                if not st.session_state.get('auto_dwg_to_dxf', True):
                                    errors.append(f'{f.name}: automatyczna konwersja DWG → DXF jest wyłączona.')
                                else:
                                    try:
                                        dxf_data, converter = auto_convert_dwg_to_dxf(f.name, data, audit=True)
                                        dxf_rows, dxf_meta = analyze_dxf(f.name, dxf_data, st.session_state.patterns, st.session_state.exclude_legend, st.session_state.dxf_count_mode)
                                        rows.extend(dxf_rows)
                                        st.session_state.setdefault('converted_dxf', {})[_file_content_cache_key(f)] = dxf_data
                                        _event(f'DWG → DXF: {f.name} — konwersja przez {Path(converter).parent.name}; znaleziono {len(dxf_rows)} pozycji.', 'success'); analysis_status.write(f'  DWG → DXF: automatycznie przez {Path(converter).parent.name}; znaleziono {len(dxf_rows)} pozycji.')
                                        if not dxf_meta.empty:
                                            for _, m in dxf_meta.iterrows():
                                                statuses.setdefault(f.name, []).append(f'Layout {m["layout"]}: wykluczono bloki z legendy')
                                    except Exception as conv_exc:
                                        errors.append(f'{f.name}: automatyczna konwersja DWG → DXF: {type(conv_exc).__name__}: {conv_exc}')
                                        _event(f'DWG → DXF BŁĄD: {f.name}: {type(conv_exc).__name__}: {conv_exc}', 'error'); analysis_status.write(f'  DWG → DXF BŁĄD: {type(conv_exc).__name__}: {conv_exc}')
                            else:
                                errors.append(f'{f.name}: nieobsługiwany format {ext}.')
                        except Exception as exc:
                            msg=f'{f.name}: {type(exc).__name__}: {exc}'
                            errors.append(msg); _event(f'BŁĄD: {msg}', 'error'); analysis_status.write(f'  BŁĄD: {msg}')
                        _file_elapsed = time.perf_counter() - _file_started
                        _file_timings.append((f.name, _file_elapsed))
                        _event(f'Czas pliku: {f.name} — {_file_elapsed:.2f} s.', 'info')
                        analysis_status.write(f'  Czas pliku: {f.name} — {_file_elapsed:.2f} s.')
                        bar.progress((i+1)/total_files)

                    st.session_state.pdf_pages=pdf_pages
                    st.session_state.pdf_status=statuses
                    st.session_state.results=pd.DataFrame(rows, columns=RESULT_COLUMNS)
                    st.session_state.legend_maps={}
                    for f in files:
                        try:
                            ext = Path(f.name).suffix.lower()
                            if ext == '.dxf':
                                st.session_state.legend_maps[f.name]=_legend_display_name_map(f.getvalue(), exclude_legend=st.session_state.exclude_legend)
                            elif ext == '.dwg':
                                cache_key = _file_content_cache_key(f)
                                dxf_data = st.session_state.get('converted_dxf', {}).get(cache_key)
                                if dxf_data:
                                    st.session_state.legend_maps[f.name]=_legend_display_name_map(dxf_data, exclude_legend=st.session_state.exclude_legend)
                        except Exception as exc:
                            st.session_state.legend_maps[f.name]={}
                            errors.append(f'{f.name}: mapa legendy: {type(exc).__name__}: {exc}')
                    st.session_state.results=apply_legend_names_to_results(st.session_state.results, st.session_state.legend_maps)
                    total=int(st.session_state.results['Ilość'].sum()) if not st.session_state.results.empty else 0
                    try:
                        _dxf_for_export, _ = get_dxf_files_with_auto_conversion(files, auto=st.session_state.get('auto_dwg_to_dxf', True))
                        st.session_state.xlsx_bytes=export_xlsx(st.session_state.results, uploaded_files=list(files)+_dxf_for_export, exclude_legend=st.session_state.exclude_legend)
                        _event('XLSX: przygotowanie zakończone.', 'success'); analysis_status.write('XLSX: przygotowanie zakończone.')
                    except Exception as exc:
                        st.session_state.xlsx_bytes=None
                        errors.append(f'XLSX: {type(exc).__name__}: {exc}')
                        _event(f'XLSX BŁĄD: {type(exc).__name__}: {exc}', 'error'); analysis_status.write(f'XLSX BŁĄD: {type(exc).__name__}: {exc}')
                    _analysis_elapsed = time.perf_counter() - _analysis_started
                    st.session_state.analysis_timings = {
                        'total_seconds': round(_analysis_elapsed, 3),
                        'files': [{'name': n, 'seconds': round(sec, 3)} for n, sec in _file_timings],
                    }
                    _event(f'Łączny czas analizy i przygotowania eksportu: {_analysis_elapsed:.2f} s.', 'success')
                    analysis_status.write(f'Łączny czas analizy i przygotowania eksportu: {_analysis_elapsed:.2f} s.')
                    analysis_status.update(label=('Analiza zakończona — sprawdź komunikaty.' if errors else 'Analiza zakończona.'), state=('error' if errors else 'complete'))
            except Exception as exc:
                msg=f'KRYTYCZNY BŁĄD ANALIZY: {type(exc).__name__}: {exc}'
                errors.append(msg); st.session_state.analysis_last_error=msg; _event(msg, 'error'); st.error(msg)
            finally:
                st.session_state.analysis_running=False
            for msg in errors:
                st.error(msg)
            if not errors:
                st.success(f'Analiza zakończona. Wykryto {int(st.session_state.results["Ilość"].sum()) if not st.session_state.results.empty else 0} elementów.')

            # Po zakończeniu analizy wymuś jeden rerun, aby odświeżyć wspólny
            # pasek postępu, statystyki oraz przycisk Pobierz Excel.
            # Bez tego Streamlit pozostaje na stanie renderowania sprzed analizy
            # (np. etap 1 i nieaktywny download), mimo że dane zostały już zapisane.
            st.rerun()

        for fname, info in st.session_state.pdf_status.items():
            with st.expander(f'Kontrola PDF: {fname}'):
                st.text('\n'.join(info))

        res = st.session_state.results
        if not res.empty:
            view_cols = ['Plik', 'Strona', 'Typ elementu', 'Nazwa bloku', 'Nazwa z legendy',
                         'Symbol/etykieta', 'Ilość', 'Metoda', 'Pewność', 'Źródło']
            view = res.sort_values(['Plik', 'Strona', 'Typ elementu', 'Symbol/etykieta']).copy()
            for col in view_cols:
                if col not in view.columns:
                    view[col] = ''
            # Tabela interaktywna: kliknięcie wiersza wybiera element dla modułu podglądu.
            # To jest celowe zamiast osobnego selectboxa — użytkownik zaznacza pozycję bezpośrednio w zestawieniu.
            view_reset = view.reset_index(drop=True)
            table_view = view_reset[view_cols].copy()
            try:
                table_event = st.dataframe(
                    table_view, use_container_width=True, hide_index=True,
                    on_select="rerun", selection_mode="single-row", key="elements_results_table"
                )
                selected_rows = list(getattr(getattr(table_event, "selection", None), "rows", []) or [])
            except TypeError:
                st.dataframe(table_view, use_container_width=True, hide_index=True)
                selected_rows = []
            # Fallback/explicit selection — keeps the preview usable on older Streamlit versions.
            if len(table_view) > 0 and not selected_rows:
                labels=[]
                for i,r in table_view.iterrows():
                    labels.append(f"{i+1}. {r.get('Nazwa z legendy','') or r.get('Nazwa bloku','')} — {r.get('Nazwa bloku','')}")
                current=int(st.session_state.get('selected_element_preview_idx',0))
                current=min(max(current,0),len(labels)-1)
                chosen=st.selectbox('Element do podglądu', labels, index=current, key='preview_element_selector')
                selected_rows=[labels.index(chosen)]

            # --- Moduł podglądu zaznaczonego elementu ---
            st.markdown('<div class="th-section-title">Podgląd zaznaczonego elementu</div>', unsafe_allow_html=True)
            if not selected_rows:
                # Zachowaj wybór pomiędzy rerunami i pokaż pierwszy element jako stan początkowy.
                selected_idx = int(st.session_state.get("selected_element_preview_idx", 0))
                if selected_idx >= len(table_view):
                    selected_idx = 0
            else:
                selected_idx = int(selected_rows[0])
                st.session_state["selected_element_preview_idx"] = selected_idx

            if len(table_view):
                selected = view_reset.iloc[selected_idx]
                p1,p2,p3=st.columns([0.725,1.5125,1.5125], gap='small')
                with p1:
                    st.markdown('<div class="th-preview-card"><div class="th-preview-title">Podgląd zaznaczonego elementu</div>', unsafe_allow_html=True)
                    preview_bytes=None
                    try:
                        src_name=str(selected.get('Plik',''))
                        src_files=[f for f in files if f.name==src_name and Path(f.name).suffix.lower()=='.dxf']
                        preview_data = src_files[0].getvalue() if src_files else None
                        if not preview_data:
                            for src in files:
                                if src.name == src_name and Path(src.name).suffix.lower() == '.dwg':
                                    cache_key = _file_content_cache_key(src)
                                    preview_data = st.session_state.get('converted_dxf', {}).get(cache_key)
                                    break
                        if preview_data:
                            preview_bytes=render_selected_insert_png(
                                preview_data,
                                selected.get('Nazwa bloku',''),
                                exclude_legend=st.session_state.exclude_legend,
                                entity_handle=selected.get('_PreviewHandle','')
                            )
                    except Exception:
                        preview_bytes=None
                    if preview_bytes:
                        st.image(preview_bytes, width=220)
                    else:
                        st.markdown('<div class="th-preview-empty">Brak podglądu geometrii dla tego elementu.</div>', unsafe_allow_html=True)
                    st.markdown('</div>', unsafe_allow_html=True)
                with p2:
                    st.markdown('<div class="th-info-card"><div class="th-preview-title">Informacje o elemencie</div>', unsafe_allow_html=True)
                    info_rows=[
                        ('Typ elementu', selected.get('Typ elementu','')),
                        ('Nazwa bloku', selected.get('Nazwa bloku','')),
                        ('Nazwa z legendy', selected.get('Nazwa z legendy','')),
                        ('Symbol/etykieta', selected.get('Symbol/etykieta','')),
                        ('Ilość', selected.get('Ilość','')),
                        ('Metoda', selected.get('Metoda','')),
                        ('Pewność', selected.get('Pewność','')),
                        ('Źródło', selected.get('Źródło','')),
                    ]
                    for lab,val in info_rows:
                        st.markdown(f'<div class="th-info-row"><span>{lab}:</span><b>{val}</b></div>', unsafe_allow_html=True)
                    st.markdown('</div>', unsafe_allow_html=True)
                with p3:
                    st.markdown('<div class="th-graphic-card"><div class="th-preview-title">Oznaczenie graficzne (z legendy)</div>', unsafe_allow_html=True)
                    legend_png=None
                    try:
                        src_name=str(selected.get('Plik',''))
                        wanted=[str(selected.get('Nazwa bloku',''))]
                        _dxf_for_graphics, _ = get_dxf_files_with_auto_conversion(files, auto=st.session_state.get('auto_dwg_to_dxf', True))
                        legend_graphics=collect_legend_graphics(list(files)+_dxf_for_graphics, desired_blocks=wanted, exclude_legend=st.session_state.exclude_legend)
                        legend_png=legend_graphics.get(str(selected.get('Nazwa bloku','')))
                    except Exception:
                        legend_png=None
                    if legend_png:
                        st.image(legend_png, width=220)
                    else:
                        st.markdown('<div class="th-preview-empty">Brak symbolu w legendzie.</div>', unsafe_allow_html=True)
                    st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.caption('Brak automatycznie wykrytych elementów. PDF analizuj przez tekst/OCR; DXF jest zliczany wyłącznie z referencji do bloków CAD (INSERT), a nazwa każdego bloku jest zachowana w kolumnie „Nazwa bloku”.')

    if active_module == 1:
        st.markdown('### 📏 Pomiar tras kablowych')
        if _is_cad:
                st.caption('Zakres pomiaru CAD: wyłącznie Model Space — Layout / Paper Space pominięto.')
                st.caption('Pomiar długości geometrii LINE/LWPOLYLINE/POLYLINE w DXF. Wynik jest grupowany wg warstwy i przestrzeni Model.')
                dxf_files, _measure_conv_msgs = get_dxf_files_with_auto_conversion(files, auto=st.session_state.get('auto_dwg_to_dxf', True))
                for _n, _ok, _msg in _measure_conv_msgs:
                    (st.success if _ok else st.warning)(_msg)
                if not dxf_files:
                    st.info('Wczytaj plik DXF, aby rozpocząć pomiar.')
                else:
                    # Można mierzyć pojedynczy plik albo wszystkie pliki DXF/DWG
                    # wczytane do analizy. Dla opcji „Wszystkie” wynik zachowuje
                    # kolumnę „Plik”, dzięki czemu długości pozostają rozdzielone.
                    _measure_all_token = '__ALL_MEASURE_FILES__'
                    _measure_options = [_measure_all_token] + list(dxf_files)
                    mf = st.selectbox(
                        'Plik DXF do pomiaru',
                        _measure_options,
                        format_func=lambda f: '📐 Wszystkie pliki wczytane do analizy' if f == _measure_all_token else f.name,
                        key='measure_file_228'
                    )
                    _measure_all = mf == _measure_all_token
                    _measure_targets = list(dxf_files) if _measure_all else [mf]
                    _scope_target = st.selectbox('Plik do kontroli zakresu (Trasy kablowe)', _measure_targets, format_func=lambda f: f.name, key='routes_scope_file_26375')
                    _render_model_layout_scope_audit(_scope_target, 'routes26375')
    
                    # Dla opcji „Wszystkie” lista warstw jest sumą warstw wszystkich plików.
                    measurable_layers = set()
                    _layers_status = st.empty()
                    _layers_progress = st.progress(0, text='Przygotowanie odczytu DXF…')
                    for _layer_i, _target in enumerate(_measure_targets, start=1):
                        _size_mb = len(_target.getvalue()) / (1024 * 1024)
                        _layers_status.info(f'⏳ Wczytywanie DXF: **{_target.name}** ({_size_mb:.1f} MB) — odczyt warstw i przygotowanie pomiaru…')
                        try:
                            with st.spinner(f'Analizuję warstwy pliku {_target.name}…'):
                                measurable_layers.update(measurable_dxf_layers(_target.getvalue()))
                        except Exception as exc:
                            st.warning(f'Nie udało się odczytać warstw z {_target.name}: {type(exc).__name__}: {exc}')
                        _layers_progress.progress(_layer_i / len(_measure_targets), text=f'Odczyt warstw DXF: {_layer_i}/{len(_measure_targets)}')
                    _layers_progress.empty()
                    _layers_status.success(f'✓ DXF gotowy do pomiaru. Plików: {len(_measure_targets)}; rozpoznanych warstw: {len(measurable_layers)}.')
    
                    layers = sorted(x for x in measurable_layers if x)
                    preferred = [x for x in layers if re.search(r'(TRAS|TRASA|KORYTO|KANAL|DRAB|TELETECH|ELE)', x, re.I)]
                    selected = st.multiselect(
                        'Warstwy do pomiaru',
                        options=layers,
                        default=preferred or layers,
                        key='measure_layers_228'
                    )
                    st.markdown('#### Trasy kablowe — właściwości bloków dynamicznych')
                    st.caption('Opcjonalnie wczytaj szczegółowy CSV z właściwościami dynamicznymi bloków tras kablowych. Obecnie importer rozpoznaje typy KK 100/200/300/400; nazwa warstwy w dokumentacji może być dowolna. Bez CSV dotychczasowy pomiar geometrii działa bez zmian.')
                    _lan_ladder_csv = st.file_uploader(
                        'Eksport właściwości tras kablowych (CSV)',
                        type=['csv'], key='lan_ladder_csv_228'
                    )
                    _include_block_geometry = st.checkbox(
                        'Uwzględnij geometrię wewnątrz bloków INSERT',
                        value=False,
                        key='measure_include_block_geometry_228',
                        help='Włącz tylko wtedy, gdy rzeczywisty przebieg trasy jest zapisany wewnątrz bloków. Domyślnie mierzone są wyłącznie obiekty liniowe bezpośrednio w układzie rysunku.'
                    )
    
                    c1, c2 = st.columns(2)
                    scale = c1.number_input(
                        'Przelicznik jednostek CAD → m',
                        min_value=0.000001,
                        value=0.001,
                        format='%.6f',
                        help='Dla DXF w milimetrach: 0,001. Dla DXF w metrach: 1,0.'
                    )
                    if c2.button('📐 Oblicz długości', type='primary', use_container_width=True):
                        st.session_state.measure_error = ''
                        # Export files are expensive to rebuild on every Streamlit rerun.
                        # Clear this bundle only when a new measurement is explicitly requested.
                        st.session_state['_measure_export_cache'] = None
                        _selected_layers = set(selected) if selected else None
                        _parts = []
                        _errors = []
                        st.session_state['_lan_csv_dxf_audit'] = pd.DataFrame()
                        _audit_parts = []
                        # Parse the properties CSV before geometry measurement. Its own
                        # layer names determine which CAD layers must be skipped to avoid
                        # counting the same routes once as geometry and again as block data.
                        _route_property_rows = pd.DataFrame()
                        _route_csv_layers = set()
                        if _lan_ladder_csv is not None:
                            try:
                                _route_property_rows = measure_lan_ladder_csv(
                                    _lan_ladder_csv.name, _lan_ladder_csv.getvalue()
                                )
                                if not _route_property_rows.empty and 'Warstwa' in _route_property_rows.columns:
                                    _route_csv_layers = {
                                        str(v).strip() for v in _route_property_rows['Warstwa'].dropna()
                                        if str(v).strip()
                                    }
                            except Exception as _csv_exc:
                                st.warning(f'Nie udało się odczytać CSV tras kablowych: {type(_csv_exc).__name__}: {_csv_exc}')
                        _active_csv_layers = (_route_csv_layers if not _selected_layers else (_route_csv_layers & _selected_layers))
                        _measure_status = st.empty()
                        _measure_progress = st.progress(0, text='Rozpoczynam pomiar tras…')
                        for _target_i, _target in enumerate(_measure_targets, start=1):
                            _target_mb = len(_target.getvalue()) / (1024 * 1024)
                            _measure_status.info(f'⏳ Pomiar DXF {_target_i}/{len(_measure_targets)}: **{_target.name}** ({_target_mb:.1f} MB) — analizuję geometrię i bloki…')
                            try:
                                if _lan_ladder_csv is not None:
                                    _audit = audit_lan_ladder_csv_dxf(
                                        _lan_ladder_csv.name, _lan_ladder_csv.getvalue(),
                                        _target.name, _target.getvalue(), _selected_layers
                                    )
                                    if not _audit.empty:
                                        _audit_parts.append(_audit)
                                _target_digest = hashlib.sha1(_target.getvalue()).hexdigest()[:12]
                                _scope_excluded = set(st.session_state.get(f'routes26375_scope_{_target_digest}_excluded_layers', []))
                                _route_ignore_layers = set(_active_csv_layers or set()) | _scope_excluded
                                _part = measure_dxf_lengths(
                                    _target.name, _target.getvalue(), unit_scale=scale,
                                    selected_layers=_selected_layers,
                                    # Exclusions chosen in Model/Layout audit are layer-wide;
                                    # they are merged with CSV layers suppressed to avoid double counting.
                                    ignore_layers=_route_ignore_layers or None,
                                    include_block_geometry=_include_block_geometry,
                                    recognize_route_blocks=(_lan_ladder_csv is None)
                                )
                                if not _part.empty:
                                    _parts.append(_part)
                            except Exception as _exc:
                                _errors.append(f'{_target.name}: {type(_exc).__name__}: {_exc}')
                            _measure_progress.progress(_target_i / len(_measure_targets), text=f'Pomiar tras: {_target_i}/{len(_measure_targets)} plików')
                        _measure_progress.empty()
                        _measure_status.success(f'✓ Odczyt i pomiar DXF zakończony. Przetworzono plików: {len(_measure_targets)}.')
                        if _errors:
                            st.session_state.measure_error = '\n'.join(_errors)
                            for _err in _errors:
                                st.warning(_err)
                        # Without the AutoCAD dynamic-property CSV, direct-geometry mode
                        # intentionally avoids expanding INSERT graphics. Warn the user
                        # when selected layers contain INSERTs, because the geometry total
                        # alone may then be incomplete for dynamic cable-tray blocks.
                        if _lan_ladder_csv is None and not _include_block_geometry:
                            _insert_counts = {}
                            for _target in _measure_targets:
                                _insert_counts.update(count_dxf_inserts_by_layer(
                                    _target.name, _target.getvalue(), _selected_layers
                                ))
                            _insert_total = sum(_insert_counts.values())
                            if _insert_total:
                                _examples = ', '.join(
                                    f'{layer}: {block} ({count})'
                                    for (layer, block), count in sorted(_insert_counts.items())[:8]
                                )
                                st.warning(
                                    f'Na wybranych warstwach znaleziono {_insert_total} bloków INSERT, ale nie wczytano CSV właściwości dynamicznych. '
                                    'Thanos próbuje rozpoznać trasy w blokach po parze długich, równoległych szyn; grafika pomocnicza nie jest sumowana w całości, ale wynik geometrii nadal traktuj jako kontrolny. '
                                    'Wczytaj szczegółowy CSV z EKSPORTKK V5, aby ująć długości dynamiczne bez doliczania grafiki bloków. '
                                    f'Przykładowe bloki: {_examples}'
                                )
                        # Add parsed dynamic-block route lengths as a separate, traceable
                        # measurement source. Existing measurements of other layers remain.
                        if not _route_property_rows.empty and (not _selected_layers or bool(_route_csv_layers & _selected_layers)):
                            _parts.append(_route_property_rows)
                        elif _lan_ladder_csv is not None and _route_property_rows.empty:
                            st.warning('CSV nie zawiera rozpoznanych właściwości długości KK 100/200/300/400. Sprawdź kolumny właściwości dynamicznych i długości.')
                        st.session_state.measurements = (
                            pd.concat(_parts, ignore_index=True, sort=False) if _parts else pd.DataFrame()
                        )
                        st.session_state['_lan_csv_dxf_audit'] = (
                            pd.concat(_audit_parts, ignore_index=True, sort=False) if _audit_parts else pd.DataFrame()
                        )
    
                    _match_audit = st.session_state.get('_lan_csv_dxf_audit', pd.DataFrame())
                    if not _match_audit.empty:
                        with st.expander('Diagnostyka CSV ↔ DXF — dopasowanie po uchwycie', expanded=False):
                            _audit_counts = _match_audit['Status dopasowania'].value_counts(dropna=False).to_dict()
                            st.caption('Liczba rekordów wg statusu dopasowania: ' + '; '.join(f'{k}: {v}' for k, v in _audit_counts.items()))
                            _type_mismatch = _match_audit[_match_audit['Status dopasowania'].astype(str).eq('ZGODNY_UCHWYT_ROZNY_TYP')]
                            _missing_length = _match_audit[_match_audit['Status dopasowania'].astype(str).eq('BRAK_DLUGOSCI')]
                            _csv_only = _match_audit[_match_audit['Status dopasowania'].astype(str).eq('CSV_BEZ_BLOKU_DXF')]
                            _dxf_only = _match_audit[_match_audit['Status dopasowania'].astype(str).eq('BLOK_DXF_BEZ_REKORDU_CSV')]
                            if not _type_mismatch.empty:
                                st.warning(f'Rozbieżność TYP ↔ właściwość dynamiczna: {_type_mismatch.shape[0]} rekord(y). To ostrzeżenie diagnostyczne — Thanos nie zmienia automatycznie typu ani długości. Sprawdź uchwyty i oba pola typu w tabeli poniżej.')
                                st.dataframe(_type_mismatch[['Uchwyt', 'Blok CSV', 'Blok DXF', 'TYP atrybutu', 'Typ właściwości', 'Długość CSV [m]', 'Status dopasowania']], use_container_width=True, hide_index=True)
                            if not _missing_length.empty:
                                st.warning(f'Brak długości w CSV: {_missing_length.shape[0]} rekord(y). Nie doliczono ich do metrów tras.')
                            if not _csv_only.empty:
                                st.warning(f'Rekordy CSV bez odpowiednika INSERT w wybranych warstwach DXF: {_csv_only.shape[0]}. Sprawdź wybór pliku/warstw.')
                            if not _dxf_only.empty:
                                st.info(f'Bloki INSERT bez rekordu CSV: {_dxf_only.shape[0]}. Nie dopisano ich automatycznie do sumy długości.')
                            st.dataframe(_match_audit, use_container_width=True, hide_index=True)
                            _audit_csv = _match_audit.to_csv(index=False, sep=';', encoding='utf-8-sig').encode('utf-8-sig')
                            st.download_button('Pobierz diagnostykę CSV ↔ DXF', _audit_csv,
                                file_name='Thanos_diagnostyka_CSV_DXF.csv', mime='text/csv', key='download_lan_match_audit')

                    md = st.session_state.measurements
                    if not md.empty:
                        md = md.copy()
                        summary_cols = ['Koryto', 'Szerokość [mm]', 'Długość [m]', 'Obiekty', 'Jednostka', 'Ilość [szt.]']
                        _geometry_type = md.get('Typ geometrii', pd.Series('', index=md.index)).astype(str)
                        _dynamic_mask = _geometry_type.str.contains('Właściwość dynamiczna bloku', case=False, na=False)
                        _piece_mask = _geometry_type.str.contains('Element ilościowy', case=False, na=False)
                        _dynamic_md = md[_dynamic_mask].copy()
                        _piece_md = md[_piece_mask].copy()
                        _geometry_md = md[~(_dynamic_mask | _piece_mask)].copy()
                        # Explain the provenance of each measurement source and flag
                        # any layer that appears in both CSV block lengths and CAD geometry.
                        _source_audit = validate_measurement_sources(md)
                        st.caption(
                            f"Kontrola źródeł: {_source_audit['dynamic_rows']} rekordów z CSV właściwości, "
                            f"{_source_audit['piece_rows']} elementów ilościowych i "
                            f"{_source_audit['geometry_rows']} rekordów geometrii CAD."
                        )
                        if _source_audit['overlap_layers']:
                            st.warning(
                                'Wykryto warstwy obecne jednocześnie w CSV właściwości i pomiarze geometrii: '
                                + ', '.join(_source_audit['overlap_layers'])
                                + '. Sprawdź, czy te same obiekty nie zostały policzone dwukrotnie.'
                            )
                        for _frame in (_dynamic_md, _piece_md, _geometry_md):
                            if 'Jednostka' not in _frame.columns:
                                _frame['Jednostka'] = 'm'
                            else:
                                _frame['Jednostka'] = _frame['Jednostka'].fillna('m')
                            if 'Ilość [szt.]' not in _frame.columns:
                                _frame['Ilość [szt.]'] = 0
                            else:
                                _frame['Ilość [szt.]'] = _frame['Ilość [szt.]'].fillna(0)

                        if not _dynamic_md.empty:
                            st.markdown('#### Trasy kablowe — typ z właściwości dynamicznych')
                            _lan_summary = _dynamic_md.groupby(['Koryto', 'Szerokość [mm]', 'Jednostka'], dropna=False, as_index=False).agg(
                                **{'Długość [m]': ('Długość [m]', 'sum'),
                                   'Obiekty': ('Obiekty', 'sum'),
                                   'Ilość [szt.]': ('Ilość [szt.]', 'sum')}
                            )
                            _lan_summary['_sort_width'] = pd.to_numeric(_lan_summary['Szerokość [mm]'], errors='coerce')
                            _lan_summary = _lan_summary.sort_values('_sort_width', na_position='last').drop(columns=['_sort_width'])
                            st.dataframe(_lan_summary[summary_cols], use_container_width=True, hide_index=True)
                            st.caption('Zestawienie obejmuje odcinki tras rozpoznane z właściwości dynamicznych; elementy ilościowe liczone w sztukach są pokazane osobno.')

                        if not _piece_md.empty:
                            st.markdown('#### Uchwyty i elementy ilościowe — sztuki')
                            _piece_summary = _piece_md.groupby(['Koryto', 'Jednostka'], as_index=False).agg(
                                **{'Ilość [szt.]': ('Ilość [szt.]', 'sum'), 'Obiekty': ('Obiekty', 'sum')}
                            )
                            st.dataframe(_piece_summary, use_container_width=True, hide_index=True)

                        if not _geometry_md.empty:
                            st.markdown('#### Pozostałe trasy — pomiar geometrii')
                            _geometry_summary = _geometry_md.groupby(['Koryto', 'Szerokość [mm]', 'Jednostka'], dropna=False, as_index=False).agg(
                                **{'Długość [m]': ('Długość [m]', 'sum'),
                                   'Obiekty': ('Obiekty', 'sum'),
                                   'Ilość [szt.]': ('Ilość [szt.]', 'sum')}
                            )
                            _geometry_summary['_sort_width'] = pd.to_numeric(_geometry_summary['Szerokość [mm]'], errors='coerce')
                            _geometry_summary = _geometry_summary.sort_values('_sort_width', na_position='last').drop(columns=['_sort_width'])
                            st.dataframe(_geometry_summary[summary_cols], use_container_width=True, hide_index=True)
                            st.caption('Pozycja „Nie określono” oznacza geometrię, której typu koryta nie udało się ustalić na podstawie warstwy/obiektu.')

                        st.markdown('#### Szczegółowo wg warstwy')
                        st.dataframe(
                            md.assign(_sort_width=pd.to_numeric(md['Szerokość [mm]'], errors='coerce'))
                          .sort_values(['_sort_width', 'Warstwa', 'Długość [m]'],
                                       ascending=[True, True, False], na_position='last')
                          .drop(columns=['_sort_width']),
                            use_container_width=True,
                            hide_index=True
                        )
                        total = float(md['Długość [m]'].sum())
                        st.metric('Łączna długość', f'{total:,.2f} m'.replace(',', ' '))
    
                        # Build all route exports through one tested helper, then cache until a new measurement.
                        _export_bundle = st.session_state.get('_measure_export_cache')
                        if not _export_bundle:
                            _export_bundle = build_route_measurement_exports(md, summary_cols)
                            st.session_state['_measure_export_cache'] = _export_bundle

                        st.download_button(
                            '⬇ Pobierz pomiar tras Excel',
                            data=_export_bundle['xlsx'],
                            file_name=_xlsx_filename('Thanos_pomiar_tras'),
                            mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            key='download_measure_xlsx_228'
                        )
                        # CSV exports complement XLSX for downstream costing/import workflows.
                        _detail_csv = _export_bundle['detail_csv']
                        _summary_csv = _export_bundle['summary_csv']
                        csv_c1, csv_c2 = st.columns(2)
                        csv_c1.download_button(
                            '⬇ Pobierz szczegóły CSV',
                            data=_detail_csv,
                            file_name='Thanos_pomiar_tras_szczegoly.csv',
                            mime='text/csv; charset=utf-8',
                            key='download_measure_csv_detail_228'
                        )
                        csv_c2.download_button(
                            '⬇ Pobierz sumy CSV',
                            data=_summary_csv,
                            file_name='Thanos_pomiar_tras_sumy.csv',
                            mime='text/csv; charset=utf-8',
                            key='download_measure_csv_summary_228'
                        )
                    else:
                        if st.session_state.get('measure_error'):
                            st.error('Błąd pomiaru: ' + st.session_state.measure_error)
                        else:
                            st.warning('Brak zmierzonej geometrii. Sprawdź wybrane warstwy oraz przelicznik jednostek.')
        else:
                st.caption('Pomiar tras z PDF wektorowego. Thanos mierzy rzeczywistą geometrię wektorową PDF — bez potrzeby porównywania z DXF.')
                if not files:
                    st.info('Wczytaj plik PDF, aby rozpocząć pomiar.')
                else:
                    pdf_measure_file = st.selectbox('Plik PDF do pomiaru', files, format_func=lambda f:f.name, key='pdf_measure_file_263')
                    raw_pdf = pdf_measure_file.getvalue()
                    auto_scale = detect_pdf_scale(raw_pdf, 100.0)
                    c1,c2,c3,c4=st.columns(4)
                    scale_den=c1.number_input('Skala rysunku 1:',min_value=1.0,value=float(auto_scale),step=1.0,key='pdf_measure_scale_263')
                    unit_scale=c2.number_input('Jednostka modelu → m',min_value=0.000001,value=0.001,format='%.6f',key='pdf_measure_unit_263',help='Dla skali drukowanej PDF: pozostaw 0,001, ponieważ geometria jest mierzona w mm modelu po przeliczeniu skali.')
                    route_types=c3.multiselect('Trasy', ['ELE','TELE'], default=['ELE','TELE'], key='pdf_route_types_263', format_func=lambda x: {'ELE':'🟢 Koryto elektryczne','TELE':'🔵 Koryto teletechniczne'}[x])
                    group_labels=c4.checkbox('Podział wg oznaczeń',value=True,key='pdf_route_group_labels_263',help='Rozdziela długość tras wg oznaczeń typu K100H60/ELE, K200H60/TELE, K300H60/ELE.')
                    if st.button('📐 Oblicz długości tras PDF',type='primary',use_container_width=True,key='pdf_measure_run_263'):
                        try:
                            st.session_state.pdf_measurements=measure_pdf_routes(raw_pdf,scale_denominator=scale_den,unit_scale=unit_scale,selected_routes=route_types,group_by_labels=group_labels)
                            st.session_state.pdf_measure_error=''
                        except Exception as exc:
                            st.session_state.pdf_measurements=pd.DataFrame(); st.session_state.pdf_measure_error=f'{type(exc).__name__}: {exc}'
                    pmd=st.session_state.get('pdf_measurements',pd.DataFrame())
                    if not pmd.empty:
                        st.markdown('#### Zestawienie długości tras wg oznaczeń')
                        st.dataframe(pmd,use_container_width=True,hide_index=True)
                        if 'Oznaczenie' in pmd.columns:
                            _route_summary=pmd.groupby(['Oznaczenie','Trasa','Kod'],as_index=False).agg(**{'Długość [m]':('Długość [m]','sum'),'Obiekty':('Obiekty','sum')})
                            st.markdown('#### Podsumowanie wg oznaczenia')
                            st.dataframe(_route_summary,use_container_width=True,hide_index=True)
                        st.metric('Łączna długość tras',f"{float(pmd['Długość [m]'].sum()):,.2f} m".replace(',',' '))
                        bio=io.BytesIO(); pmd.to_excel(bio,index=False,engine='openpyxl')
                        st.download_button('⬇ Pobierz pomiar tras PDF Excel',data=bio.getvalue(),file_name=_xlsx_filename(Path(pdf_measure_file.name).stem+'_pomiar_tras_PDF'),mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',key='download_pdf_measure_xlsx_263')
                    elif st.session_state.get('pdf_measure_error'):
                        st.error('Błąd pomiaru PDF: '+st.session_state.pdf_measure_error)
                    else:
                        st.info(f'Wykryta skala w rysunku: 1:{int(auto_scale)}. Pomiar wykorzysta geometrię wektorową PDF.')
    
    if active_module == 2:
        st.markdown('### ⚡ Odgrom/uziom')
        if _is_cad:
                st.caption('Pomiar długości przewodów odgromowych i uziomu oraz zliczanie podstawowych elementów osprzętu z DXF.')
                dxf_files_og, _og_conv_msgs = get_dxf_files_with_auto_conversion(files, auto=st.session_state.get('auto_dwg_to_dxf', True))
                for _n, _ok, _msg in _og_conv_msgs:
                    (st.success if _ok else st.warning)(_msg)
                if not dxf_files_og:
                    st.info('Wczytaj plik DXF, aby rozpocząć pomiar.')
                else:
                    ogf=st.selectbox('Plik DXF', dxf_files_og, format_func=lambda f:f.name, key='og_file_236')
                    # Stable identity is cheap for normal Streamlit uploads. Cache the
                    # discovered linear-geometry layers in session state so changing
                    # multiselect values never reparses a large DXF.
                    _og_cache_key = _file_content_cache_key(ogf)
                    _og_digest = hashlib.sha1(_og_cache_key.encode('utf-8', errors='replace')).hexdigest()[:12]
                    _og_size_mb = (getattr(ogf, 'size', None) or len(ogf.getvalue())) / (1024 * 1024)
                    _render_model_layout_scope_audit(ogf, 'odgrom26375')
                    _og_load_status = st.empty()
                    _og_layer_cache = st.session_state.setdefault('_odgrom_layer_cache_26436', {})
                    if _og_cache_key in _og_layer_cache:
                        layers = list(_og_layer_cache[_og_cache_key])
                        _og_load_status.success(f'✓ Warstwy DXF z pamięci: {len(layers)} warstw. Zmiana zaznaczenia nie wymaga ponownego odczytu pliku.')
                    else:
                        raw = ogf.getvalue()
                        _og_load_status.info(f'⏳ Pierwsze wczytywanie DXF do modułu Odgrom/uziom: **{ogf.name}** ({_og_size_mb:.1f} MB) — odczyt geometrii i warstw…')
                        opath = None
                        try:
                            import tempfile, ezdxf
                            with st.spinner(f'Odczytuję warstwy i geometrię: {ogf.name}…'):
                                with tempfile.NamedTemporaryFile(suffix='.dxf',delete=False) as tmp:
                                    tmp.write(raw); opath=Path(tmp.name)
                                odoc=ezdxf.readfile(opath)
                                layer_set=set()
                                for layout in odoc.layouts:
                                    for e in layout:
                                        if e.dxftype() in ('LINE','LWPOLYLINE','POLYLINE'):
                                            lay=str(getattr(e.dxf,'layer','') or '').strip()
                                            if lay: layer_set.add(lay)
                                layers=sorted(layer_set)
                            # Bound session memory: retain only a small number of recent files.
                            if len(_og_layer_cache) >= 8:
                                _og_layer_cache.clear()
                            _og_layer_cache[_og_cache_key] = list(layers)
                            _og_load_status.success(f'✓ DXF wczytany: {len(layers)} warstw z geometrią liniową. Warstwy zapisano w pamięci sesji.')
                        except Exception as exc:
                            layers=[]
                            st.error(f'Nie udało się odczytać DXF: {type(exc).__name__}: {exc}')
                        finally:
                            try:
                                if opath is not None: opath.unlink(missing_ok=True)
                            except Exception: pass
                    suggested=[x for x in layers if re.search(r'(ODGROM|2DE|BEDNARK|UZIO|LPS)',x,re.I)]
                    sel=st.multiselect('Warstwy odgrom/uziom do pomiaru',layers,default=suggested or layers,key='og_layers_236')
                    _og_scope_excluded = set(st.session_state.get(f'odgrom26375_scope_{_og_digest}_excluded_layers', []))
                    if _og_scope_excluded:
                        st.warning('Kontrola Model / Layout wyklucza z pomiaru całe warstwy: ' + ', '.join(sorted(_og_scope_excluded)))
                        sel = [x for x in sel if x not in _og_scope_excluded]
                    c1,c2=st.columns(2)
                    scale=c1.number_input('Przelicznik jednostek CAD → m',min_value=0.000001,value=0.001,format='%.6f',key='og_scale_236')
                    if c2.button('⚡ Oblicz odgrom/uziom',type='primary',use_container_width=True):
                        raw = ogf.getvalue()  # Read the DXF bytes only when calculation is explicitly requested.
                        st.session_state.odg_error=''
                        _og_calc_status = st.empty()
                        _og_calc_status.info(f'⏳ Pomiar odgrom/uziom: analizuję **{ogf.name}** ({_og_size_mb:.1f} MB)…')
                        try:
                            _og_calc_started = time.perf_counter()
                            with st.spinner('Obliczanie długości przewodów i zliczanie osprzętu…'):
                                st.session_state.odg_geom,st.session_state.odg_blocks=measure_odgrom_uziom(
                                    ogf.name,raw,unit_scale=scale,selected_layers=set(sel) if sel else None)
                            st.session_state.odg_calc_seconds = time.perf_counter() - _og_calc_started
                            _og_calc_status.success(
                                f'✓ Pomiar odgrom/uziom zakończony w {st.session_state.odg_calc_seconds:.2f} s.')
                        except Exception as _og_exc:
                            st.session_state.odg_error=f'{type(_og_exc).__name__}: {_og_exc}'
                            _og_calc_status.error(f'Błąd pomiaru: {st.session_state.odg_error}')
                    geom=st.session_state.odg_geom
                    bd=st.session_state.odg_blocks
                    if not geom.empty:
                        st.markdown('#### Długości')
                        gs=geom.groupby(['Kategoria'],as_index=False)['Długość [m]'].sum()
                        st.dataframe(gs,use_container_width=True,hide_index=True)
                        st.dataframe(geom.sort_values(['Kategoria','Warstwa']),use_container_width=True,hide_index=True)
                        st.metric('Łączna długość',f"{geom['Długość [m]'].sum():,.2f} m".replace(',',' '))
                    if not bd.empty:
                        st.markdown('#### Elementy/osprzęt')
                        st.dataframe(bd,use_container_width=True,hide_index=True)
                    if not geom.empty or not bd.empty:
                        import io
                        bio=io.BytesIO()
                        with pd.ExcelWriter(bio,engine='openpyxl') as writer:
                            if not geom.empty:
                                gs.to_excel(writer,index=False,sheet_name='Długości')
                                geom.to_excel(writer,index=False,sheet_name='Szczegóły')
                            if not bd.empty:
                                bd.to_excel(writer,index=False,sheet_name='Osprzęt')
                        st.download_button('⬇ Pobierz odgrom/uziom Excel',bio.getvalue(),
                                           file_name=_xlsx_filename('Thanos_odgrom_uziom'),
                                           mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                           key='og_xlsx_236')
                    elif st.session_state.get('odg_error'):
                        st.error('Błąd pomiaru: '+st.session_state.odg_error)
                    else:
                        st.info('Wybierz warstwy i kliknij „Oblicz odgrom/uziom”.')
        else:
            # PDF workspace: this branch must use PDF inputs only. Do not call
            # get_dxf_files_with_auto_conversion() or measure_odgrom_uziom().
            st.caption('Pomiar przewodów odgromowych i uziomu wyłącznie z geometrii wektorowej PDF. Pliki DXF/DWG nie są używane w tym module.')
            pdf_og_files = [f for f in files if Path(f.name).suffix.lower() == '.pdf']
            if not pdf_og_files:
                st.info('Wczytaj plik PDF w sekcji „Wybierz pliki PDF do analizy”, aby rozpocząć pomiar odgrom/uziom.')
            else:
                ogf = st.selectbox(
                    'Plik PDF do pomiaru odgrom/uziom',
                    pdf_og_files,
                    format_func=lambda f: f.name,
                    key='pdf_og_file_248'
                )
                raw_pdf_og = ogf.getvalue()
                try:
                    _auto_og_scale = detect_pdf_scale(raw_pdf_og, 100.0)
                    _og_pdf_layers = list(measurable_pdf_odgrom_layers(raw_pdf_og))
                except Exception as exc:
                    _auto_og_scale = 100.0
                    _og_pdf_layers = []
                    st.error(f'Nie udało się odczytać geometrii PDF: {type(exc).__name__}: {exc}')
                if not _og_pdf_layers:
                    st.warning('Nie wykryto w PDF warstw wektorowych odgrom/uziom. Sprawdź, czy PDF zachowuje warstwy CAD; zeskanowany PDF bez wektorów nie wystarczy do tego pomiaru.')
                _og_suggested_layers = [x for x in _og_pdf_layers if re.search(r'(ODGROM|2DE|BEDNARK|UZIO|LPS)', x, re.I)]
                _og_selected_layers = st.multiselect(
                    'Warstwy PDF odgrom/uziom do pomiaru',
                    _og_pdf_layers,
                    default=_og_suggested_layers or _og_pdf_layers,
                    key='pdf_og_layers_248'
                )
                _og_c1, _og_c2 = st.columns(2)
                _og_pdf_scale = _og_c1.number_input(
                    'Skala rysunku 1:', min_value=1.0,
                    value=float(_auto_og_scale), step=1.0,
                    key='pdf_og_scale_248',
                    help='Skala wydruku, np. 100 dla 1:100.'
                )
                _og_pdf_unit = _og_c2.number_input(
                    'Jednostka modelu → m', min_value=0.000001,
                    value=0.001, format='%.6f', key='pdf_og_unit_248',
                    help='Pozostaw 0,001, jeśli model CAD był rysowany w milimetrach.'
                )
                if st.button('⚡ Oblicz odgrom/uziom z PDF', type='primary', use_container_width=True, key='pdf_og_run_248'):
                    try:
                        st.session_state.pdf_odg_error = ''
                        st.session_state.pdf_odg_measurements = measure_pdf_odgrom_uziom(
                            raw_pdf_og,
                            scale_denominator=_og_pdf_scale,
                            unit_scale=_og_pdf_unit,
                            selected_layers=set(_og_selected_layers) if _og_selected_layers else None,
                        )
                        st.session_state.pdf_odg_source_name = ogf.name
                    except Exception as exc:
                        st.session_state.pdf_odg_measurements = pd.DataFrame()
                        st.session_state.pdf_odg_error = f'{type(exc).__name__}: {exc}'
                _og_pdf_result = st.session_state.get('pdf_odg_measurements', pd.DataFrame())
                if st.session_state.get('pdf_odg_source_name') not in (None, ogf.name):
                    _og_pdf_result = pd.DataFrame()
                if not _og_pdf_result.empty:
                    st.markdown('#### Długości z PDF')
                    _og_pdf_summary = _og_pdf_result.groupby(['Kategoria', 'Oznaczenie'], as_index=False)['Długość [m]'].sum()
                    st.dataframe(_og_pdf_summary, use_container_width=True, hide_index=True)
                    st.dataframe(_og_pdf_result, use_container_width=True, hide_index=True)
                    st.metric('Łączna długość', f"{_og_pdf_result['Długość [m]'].sum():,.2f} m".replace(',', ' '))
                    import io
                    _og_pdf_bio = io.BytesIO()
                    with pd.ExcelWriter(_og_pdf_bio, engine='openpyxl') as _og_pdf_writer:
                        _og_pdf_summary.to_excel(_og_pdf_writer, index=False, sheet_name='Podsumowanie')
                        _og_pdf_result.to_excel(_og_pdf_writer, index=False, sheet_name='Szczegóły PDF')
                    st.download_button(
                        '⬇ Pobierz pomiar odgrom/uziom PDF Excel',
                        _og_pdf_bio.getvalue(),
                        file_name=_xlsx_filename(Path(ogf.name).stem + '_odgrom_uziom_PDF'),
                        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        key='pdf_og_download_248'
                    )
                elif st.session_state.get('pdf_odg_error'):
                    st.error('Błąd pomiaru PDF: ' + st.session_state.pdf_odg_error)
                else:
                    st.info(f'Wykryta skala PDF: 1:{int(_auto_og_scale)}. Wybierz warstwy i kliknij „Oblicz odgrom/uziom z PDF”.')

    if active_module == 3 and not _is_cad:
        pages_available = st.session_state.pdf_pages
        if not pages_available:
            st.warning('Najpierw uruchom analizę PDF.')
        else:
            pdf_name = st.selectbox('Plik PDF', list(pages_available.keys()))
            pages = pages_available[pdf_name]
            page_no = st.number_input('Strona', min_value=1, max_value=len(pages), value=1, step=1)
            pg = pages[page_no - 1]
            st.image(pg['image'], caption=f'{pdf_name} — strona {page_no}', use_container_width=True)
            with st.expander('Tekst odczytany z PDF'):
                st.text(pg['text'][:30000] or 'Brak warstwy tekstowej.')


    if active_module == (3 if _is_cad else 4):
        st.markdown('### 📋 Excel → ATH')
        st.caption('Konwersja zestawienia elementów XLSX do pliku ATH dla Norma PRO / Norma EXPERT. Moduł korzysta z rzeczywistego pliku ATH jako szablonu pozycji i materiałów.')

        c1, c2 = st.columns(2)
        with c1:
            xlsx_upload = st.file_uploader('Wczytaj XLSX', type=['xlsx'], key='ath_xlsx_upload')
            use_current = False
            if st.session_state.get('xlsx_bytes'):
                use_current = st.checkbox('Użyj ostatniego XLSX wygenerowanego przez Thanosa', value=False, key='ath_use_current_xlsx')
        with c2:
            ath_upload = st.file_uploader('Wczytaj szablon ATH z Normy', type=['ath'], key='ath_template_upload')

        xlsx_bytes = None
        xlsx_name = 'Thanos'
        if use_current and st.session_state.get('xlsx_bytes'):
            xlsx_bytes = st.session_state.xlsx_bytes
            xlsx_name = st.session_state.get('project_name', 'Thanos')
        elif xlsx_upload is not None:
            xlsx_bytes = xlsx_upload.getvalue()
            xlsx_name = Path(xlsx_upload.name).stem

        ath_bytes = ath_upload.getvalue() if ath_upload is not None else None

        if xlsx_bytes and ath_bytes:
            try:
                parsed_ath = parse_ath_template(ath_bytes)
                sheets = list(pd.ExcelFile(io.BytesIO(xlsx_bytes)).sheet_names)
                default_sheet = 'Podsumowanie' if 'Podsumowanie' in sheets else sheets[0]
                sheet = st.selectbox('Arkusz źródłowy', sheets, index=sheets.index(default_sheet), key='ath_sheet')
                source_df = ath_load_excel(xlsx_bytes, sheet)
                source_cols = list(source_df.columns)
                default_col = 'Nazwa bloku' if 'Nazwa bloku' in source_cols else ('Typ elementu' if 'Typ elementu' in source_cols else source_cols[0])
                source_col = st.selectbox('Kolumna identyfikująca element', source_cols, index=source_cols.index(default_col), key='ath_source_col')

                mapping = suggest_mapping(source_df, parsed_ath, source_col)
                st.markdown('#### Mapowanie elementów')
                st.caption('Program automatycznie wyszukuje element w katalogu RMS ZEST szablonu ATH. Ten sam RMS ZEST można przypisać do wielu wierszy XLSX — każdy wiersz zostanie wyeksportowany jako osobna pozycja ATH, a ilość materiału w katalogu RMS ZEST zostanie zsumowana.')
                # Reuse a mapping filtered by the user's delete action until the
                # source XLSX/ATH inputs change (the editor key below resets then).
                _mapping_override = st.session_state.get("ath_mapping_override")
                if isinstance(_mapping_override, pd.DataFrame):
                    mapping = _mapping_override.copy()

                # Reset the editor state whenever the XLSX/ATH input changes.
                # Without a content-derived key, Streamlit can reuse an older
                # edited table (often with empty RMS ZEST values) after a new
                # file is uploaded, causing a false 'no positions' error.
                import hashlib as _hashlib
                _ath_editor_signature = _hashlib.md5(
                    xlsx_bytes + ath_bytes + str(sheet).encode('utf-8') + str(source_col).encode('utf-8')
                ).hexdigest()[:12]
                _ath_last_signature = st.session_state.get("ath_mapping_signature")
                if _ath_last_signature != _ath_editor_signature:
                    st.session_state.pop("ath_mapping_override", None)
                    st.session_state["ath_mapping_signature"] = _ath_editor_signature
                    mapping = mapping.copy()
                _ath_editor_key = 'ath_mapping_editor_' + _ath_editor_signature
                # Allow the user to remove mapping rows directly from this table.
                # The source XLSX remains untouched; only the current ATH mapping
                # is filtered before export.
                mapping = mapping.copy()
                mapping.insert(0, "Usuń", False)
                edited = st.data_editor(
                    mapping,
                    use_container_width=True,
                    hide_index=True,
                    num_rows='fixed',
                    column_config={
                        'Usuń': st.column_config.CheckboxColumn('Usuń', help='Zaznacz element do usunięcia z bieżącego mapowania ATH.'),
                        'Ilość': st.column_config.NumberColumn('Ilość', min_value=0, step=1.0),
                        'Cena materiału': st.column_config.NumberColumn('Cena materiału', min_value=0.0, step=0.01, format='%.2f'),
                        'Robocizna jedn.': st.column_config.NumberColumn('Robocizna jedn.', min_value=0.0, step=0.01, format='%.2f'),
                    },
                    key=_ath_editor_key,
                )
                if "Usuń" in edited.columns:
                    to_delete = edited["Usuń"].fillna(False).astype(bool)
                    dc1, dc2 = st.columns([1, 4])
                    with dc1:
                        delete_clicked = st.button(
                            "🗑 Usuń zaznaczone",
                            use_container_width=True,
                            key="ath_delete_selected",
                            disabled=not bool(to_delete.any()),
                        )
                    with dc2:
                        if bool(to_delete.any()):
                            st.caption(f"Zaznaczono do usunięcia: {int(to_delete.sum())} element(ów). Usunięcie dotyczy tylko mapowania ATH, nie pliku XLSX.")
                    if delete_clicked:
                        remaining = edited.loc[~to_delete].drop(columns=["Usuń"], errors="ignore").reset_index(drop=True)
                        st.session_state["ath_mapping_override"] = remaining
                        st.rerun()
                    edited = edited.drop(columns=["Usuń"], errors="ignore")

                # Mapowanie 1→N jest dozwolone: jeden RMS ZEST z szablonu ATH
                # może być przypisany do wielu pozycji XLSX.
                if not edited.empty and 'RMS ZEST' in edited.columns:
                    dup = edited[edited['RMS ZEST'].astype(str).str.strip().ne('')].copy()
                    if not dup.empty:
                        counts = dup.groupby('RMS ZEST', dropna=False).size()
                        multi = counts[counts > 1]
                        if not multi.empty:
                            st.info('Mapowanie 1→N: ' + ', '.join(f'RMS ZEST {idx} → {int(cnt)} pozycji XLSX' for idx, cnt in multi.items()))

                unmatched = edited[edited['RMS ZEST'].astype(str).str.strip().eq('')] if not edited.empty else edited
                if not unmatched.empty:
                    st.warning(f'Nieprzypisane elementy: {len(unmatched)}. Uzupełnij kolumnę „RMS ZEST” zgodnie z katalogiem poniżej.')
                with st.expander('Katalog materiałów z szablonu ATH', expanded=True):
                    st.dataframe(catalog_dataframe(parsed_ath), use_container_width=True, hide_index=True)

                st.markdown('#### Opcje eksportu ATH')
                oc1, oc2 = st.columns(2)
                with oc1:
                    use_template_prices = st.checkbox(
                        'Pobieraj ceny materiałów z szablonu ATH',
                        value=True,
                        key='ath_use_template_prices',
                        help='Dla rzeczywistych plików ATH cena materiału może być zapisana w pozycji kosztorysu (kj), a nie w polu ce katalogu. Ta opcja używa ceny z pozycji ATH.'
                    )
                with oc2:
                    append_element_name = st.checkbox(
                        'Dodaj nazwę elementu do nazwy materiału',
                        value=True,
                        key='ath_append_element_name',
                        help='Np. „Oprawa typu” + „LN16” = „Oprawa typu LN16”.'
                    )
                project_name = st.text_input('Nazwa kosztorysu', value=xlsx_name, key='ath_project_name')
                if st.button('⚡ GENERUJ ATH', type='primary', use_container_width=True, key='generate_ath_btn'):
                    try:
                        out = generate_ath(
                            ath_bytes, edited, project_name,
                            append_element_name=append_element_name,
                            use_template_prices=use_template_prices
                        )
                        st.session_state.ath_bytes = out
                        st.session_state.ath_filename = f"{Path(project_name).stem}_ATH_{datetime.now():%Y-%m-%d_%H-%M-%S}.ath"
                        st.success(f'Wygenerowano ATH: {len(out):,} B. Liczba pozycji: {int((edited["Ilość"] > 0).sum())}.')
                    except Exception as exc:
                        st.session_state.ath_bytes = None
                        if isinstance(exc, ValueError) and 'Brak pozycji możliwych' in str(exc):
                            missing = edited[edited['Ilość'].astype(float) > 0] if not edited.empty else edited
                            if not missing.empty and 'RMS ZEST' in missing.columns:
                                missing = missing[missing['RMS ZEST'].astype(str).str.strip().eq('')]
                            names = ', '.join(str(x) for x in missing.get('Element', []).tolist()[:8]) if not missing.empty else ''
                            detail = f' Nieprzypisane: {names}.' if names else ''
                            st.error(f'Nie udało się wygenerować ATH: brak poprawnego mapowania RMS ZEST.{detail} Odświeżono mapowanie dla aktualnych plików; sprawdź kolumnę „RMS ZEST”.')
                        else:
                            st.error(f'Nie udało się wygenerować ATH: {type(exc).__name__}: {exc}')

                if st.session_state.get('ath_bytes'):
                    st.download_button(
                        '⬇ Pobierz plik ATH',
                        st.session_state.ath_bytes,
                        st.session_state.get('ath_filename', 'Thanos.ath'),
                        'application/octet-stream',
                        use_container_width=True,
                        key='download_ath',
                    )
                    st.info('Plik został zapisany w formacie CP1250 i zawiera przeliczoną sekcję [SUMA KONTROLNA].')
            except Exception as exc:
                st.error(f'Nie udało się odczytać danych wejściowych: {type(exc).__name__}: {exc}')
        else:
            st.info('Wczytaj XLSX oraz rzeczywisty plik ATH z Normy. Możesz też użyć ostatniego XLSX wygenerowanego przez Thanosa.')



# Stopka aplikacji
st.markdown(f'<div class="th-footer"><span>Thanos v{APP_VERSION}</span><span>Autor aplikacji: Paweł Świerk</span></div>', unsafe_allow_html=True)
