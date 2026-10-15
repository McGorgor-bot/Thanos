"""Project-profile discovery helpers for mixed electrical documentation."""
from __future__ import annotations
import io, re, os, tempfile
from collections import Counter

ROUTE_WORDS = ('TRASA', 'TRASY', 'KORYT', 'DRABIN', 'KANAL', 'KABLOW', 'LAN', 'PESZ', 'RURA', 'PRZEWOD')
# Known route-system block/layer naming patterns (e.g. Cablofil block layer).
ROUTE_BLOCK_WORDS = ('CBIFEZ', 'CBLIFEZ', 'CABLOFIL', 'CF54', 'CF_54', 'CF 54', 'KORCF')
# These layers usually contain annotations, boundaries or height labels, not route geometry.
NON_ROUTE_WORDS = ('OPIS', 'OPISY', 'RAMKA', 'WYSOKOSC', 'WYSOKOŚĆ', 'ADRES', 'ETYKIET', 'LEGENDA', 'TABELA', 'WYMIAR')
DEVICE_WORDS = ('GNIAZD', 'OPRAW', 'LAMP', 'KAMERA', 'ACCESS_POINT', 'CZUJN', 'ROZDZ', 'WYLACZN', 'WLACZN', 'OSPRZET', 'URZADZEN', 'SYMBOL')
CIRCUIT_RE = re.compile(r'(?<![\w/])(?:[A-Z]{1,5}\s*[/.-]?\s*)?\d{1,3}(?:\.\d{1,3}){0,2}(?![\w/])', re.I)
CABLE_RE = re.compile(r'\b(?:N2XH-J|H07ZZ-F|XRUHAKXS|N2XSH|N2XH|YAKY|YDY|YKY)\b', re.I)


def _drawing_role(name: str, text_samples: list[str]) -> str:
    """Classify a drawing by filename and visible text; conservative on ambiguity."""
    label = (name + " " + " ".join(text_samples[:40])).upper()
    if any(x in label for x in ("SCHEMAT", "SCHEMATY", "JEDNOKRESK", "TABLICA OBWOD", "ROZDZIELN")):
        return "schemat"
    if any(x in label for x in ("RZUT", "PLAN INSTAL", "TRAS KABLOW", "PLANIE INSTAL")):
        return "plan"
    return "nieokreślony"


def inspect_dxf(name: str, data: bytes) -> dict:
    """Summarize Model Space layers/entities and make conservative, role-aware suggestions."""
    import ezdxf
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="thanos_profile_", suffix=".dxf", delete=False) as tmp:
            tmp.write(data); tmp_path = tmp.name
        try:
            doc = ezdxf.readfile(tmp_path)
        except Exception as first_error:
            try:
                from ezdxf.recover import readfile as recover_readfile
                doc, _auditor = recover_readfile(tmp_path)
            except Exception:
                raise first_error
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except OSError: pass
    msp = doc.modelspace()
    rows = {}; text_samples = []; block_names = Counter(); geometry_preview = {}
    for ent in msp:
        layer = str(ent.dxf.get('layer', '0') or '0')
        row = rows.setdefault(layer, {'Warstwa': layer, 'Encje': 0, 'Linie/krzywe': 0, 'Bloki': 0, 'Teksty': 0, 'Punkty': 0})
        row['Encje'] += 1; typ = ent.dxftype()
        if typ in {'LINE','LWPOLYLINE','POLYLINE','ARC','CIRCLE','SPLINE','ELLIPSE'}:
            row['Linie/krzywe'] += 1
            # Keep a bounded, lightweight sample for the UI geometry preview.
            pts = []
            try:
                if typ == 'LINE':
                    pts = [(float(ent.dxf.start.x), float(ent.dxf.start.y)), (float(ent.dxf.end.x), float(ent.dxf.end.y))]
                elif typ == 'LWPOLYLINE':
                    pts = [(float(p[0]), float(p[1])) for p in ent.get_points('xy')]
                elif typ == 'POLYLINE':
                    pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in ent.vertices]
                elif typ == 'CIRCLE':
                    c=ent.dxf.center; r=float(ent.dxf.radius); pts=[(float(c.x)+r, float(c.y)), (float(c.x)-r, float(c.y))]
                elif typ == 'ARC':
                    c=ent.dxf.center; r=float(ent.dxf.radius); pts=[(float(c.x)+r, float(c.y)), (float(c.x)-r, float(c.y))]
            except Exception: pts=[]
            if pts:
                bucket=geometry_preview.setdefault(layer, [])
                if len(bucket) < 2500: bucket.append({'typ':typ, 'punkty':pts[:300]})
        if typ == 'INSERT':
            row['Bloki'] += 1
            block_name = str(ent.dxf.get('name',''))
            block_names[block_name] += 1
            # Attribute values are useful for recognizing modular cable-tray systems.
            try:
                for attr in ent.attribs:
                    value = str(attr.dxf.get('text', '') or '')
                    if value:
                        text_samples.append(value[:180])
            except Exception:
                pass
            # Include a bounded sample of inserted block geometry in the layer preview.
            # virtual_entities() applies INSERT position/rotation/scale for a visual overlay.
            try:
                bucket = geometry_preview.setdefault(layer, [])
                if len(bucket) < 2500:
                    for virtual in ent.virtual_entities():
                        vtyp = virtual.dxftype()
                        vpts = []
                        if vtyp == 'LINE':
                            vpts = [(float(virtual.dxf.start.x), float(virtual.dxf.start.y)),
                                    (float(virtual.dxf.end.x), float(virtual.dxf.end.y))]
                        elif vtyp == 'LWPOLYLINE':
                            vpts = [(float(p[0]), float(p[1])) for p in virtual.get_points('xy')]
                        elif vtyp == 'POLYLINE':
                            vpts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in virtual.vertices]
                        elif vtyp in {'ARC','CIRCLE'}:
                            c = virtual.dxf.center; rad = float(virtual.dxf.radius)
                            if vtyp == 'CIRCLE':
                                vpts = [(float(c.x)+rad, float(c.y)), (float(c.x)-rad, float(c.y))]
                            else:
                                # A chord is enough for preview; not used for measurement.
                                vpts = [(float(c.x)+rad, float(c.y)), (float(c.x)-rad, float(c.y))]
                        if len(vpts) >= 2 and len(bucket) < 2500:
                            bucket.append({'typ': 'INSERT:'+vtyp, 'punkty': vpts[:300]})
            except Exception:
                pass
        if typ in {'TEXT','MTEXT','ATTRIB'}:
            row['Teksty'] += 1
            val = ent.dxf.get('text','') if typ != 'MTEXT' else ent.text
            if val: text_samples.append(str(val).replace('\\P',' ')[:180])
    role = _drawing_role(name, text_samples)
    joined = '\n'.join(text_samples)
    for row in rows.values():
        upper = row['Warstwa'].upper()
        route_score = 0
        block_layer_candidate = any(w in upper.replace('-', '_') for w in ROUTE_BLOCK_WORDS)
        if any(w in upper for w in NON_ROUTE_WORDS): route_score -= 8
        elif any(w in upper for w in ROUTE_WORDS): route_score += 6
        if block_layer_candidate and row['Bloki'] > 0 and role == 'plan':
            route_score += 8
            row['Sugestia'] = 'Kandydat: bloki systemu tras na planie — sprawdź geometrię'
        if row['Linie/krzywe'] >= 10: route_score += 2
        if row['Bloki'] > row['Linie/krzywe']: route_score -= 2
        # Schematic geometry is topological/symbolic, not a physical route measurement.
        if role == 'schemat': route_score -= 8
        row['Punkty'] = route_score
        if block_layer_candidate and row['Bloki'] > 0 and role == 'plan':
            row['Sugestia'] = 'Kandydat: bloki systemu tras na planie — sprawdź geometrię'
        elif route_score >= 6 and role == 'plan': row['Sugestia'] = 'Kandydat: trasa na planie'
        elif any(w in upper for w in NON_ROUTE_WORDS): row['Sugestia'] = 'Opis / ramka — nie mierzyć jako trasy'
        elif any(w in upper for w in DEVICE_WORDS) and row['Bloki'] > 0: row['Sugestia'] = 'Urządzenia / symbole — kandydat'
        elif role == 'schemat' and row['Linie/krzywe'] > 0: row['Sugestia'] = 'Geometria schematu — nie mierzyć jako trasy'
        else: row['Sugestia'] = 'Do sprawdzenia'
        row['Rola rysunku'] = role
    layer_rows = sorted(rows.values(), key=lambda x: (-x['Punkty'], -x['Linie/krzywe'], x['Warstwa'].casefold()))
    return {'plik': name, 'format': 'DXF', 'rola_rysunku': role, 'jednostki': str(doc.header.get('$INSUNITS', 0)),
            'warstwy': layer_rows, 'geometria_podgladu': geometry_preview, 'bloki': [{'Nazwa bloku': k, 'Ilość': v} for k,v in block_names.most_common(80)],
            'próbki_tekstu': text_samples[:100], 'wzorce': {'oznaczenia_obwodów': sorted({x.strip() for x in CIRCUIT_RE.findall(joined) if not re.fullmatch(r'\d+(?:[.,]\d+)?', x.strip())})[:80], 'typy_kabli': sorted(set(m.group(0).upper() for m in CABLE_RE.finditer(joined)))} }


def consolidate_pdf_route_segments(segments: list[dict], coordinate_tolerance: float = 4.0, gap_tolerance: float = 2.0, minimum_axis_length: float = 60.0) -> list[dict]:
    """Consolidate near-parallel colored PDF edges into preliminary route axes.

    Diagnostic centerline approximation only. Uses tighter coordinate and gap
    tolerances and suppresses short residuals commonly caused by labels/frames.
    It intentionally does not claim real-world units or classify tray type.
    """
    groups = {'pozioma': [], 'pionowa': []}
    for seg in segments or []:
        orient = seg.get('orientacja')
        if orient not in groups:
            continue
        try:
            x1, y1 = float(seg['x1_pkt']), float(seg['y1_pkt'])
            x2, y2 = float(seg['x2_pkt']), float(seg['y2_pkt'])
        except (KeyError, TypeError, ValueError):
            continue
        if orient == 'pozioma':
            coord = (y1 + y2) / 2.0; lo, hi = sorted((x1, x2))
        else:
            coord = (x1 + x2) / 2.0; lo, hi = sorted((y1, y2))
        if hi - lo > 0.5:
            groups[orient].append((coord, lo, hi))

    result = []
    for orient, items in groups.items():
        # Build coordinate bands; do not merge separated parallel runs merely
        # because they have the same orientation.
        items.sort(key=lambda z: z[0])
        bands = []
        for item in items:
            if not bands or abs(item[0] - (sum(x[0] for x in bands[-1]['items']) / len(bands[-1]['items']))) > coordinate_tolerance:
                bands.append({'items': [item], 'max_coord': item[0]})
            else:
                bands[-1]['items'].append(item)
                bands[-1]['max_coord'] = max(bands[-1]['max_coord'], item[0])
        for band in bands:
            coord = sum(x[0] for x in band['items']) / len(band['items'])
            intervals = sorted((x[1], x[2]) for x in band['items'])
            merged = []
            for lo, hi in intervals:
                if not merged or lo > merged[-1][1] + gap_tolerance:
                    merged.append([lo, hi])
                else:
                    merged[-1][1] = max(merged[-1][1], hi)
            for lo, hi in merged:
                length = hi - lo
                if length < minimum_axis_length:
                    continue
                if orient == 'pozioma':
                    x1, y1, x2, y2 = lo, coord, hi, coord
                else:
                    x1, y1, x2, y2 = coord, lo, coord, hi
                result.append({'x1_pkt': round(x1, 2), 'y1_pkt': round(y1, 2),
                    'x2_pkt': round(x2, 2), 'y2_pkt': round(y2, 2),
                    'długość_pkt': round(length, 2), 'orientacja': orient,
                    'liczba_scalonych_krawędzi': sum(1 for x in band['items'] if x[1] <= hi and x[2] >= lo),
                    'źródło': 'wstępna oś po scaleniu krawędzi PDF',
                    'status': 'kandydat — sprawdzić z DXF',
                    'tolerancja_osi_pkt': coordinate_tolerance})
    return sorted(result, key=lambda x: x['długość_pkt'], reverse=True)


def inspect_pdf(name: str, data: bytes) -> dict:
    """Extract text and vector-layout diagnostics from PDF without guessing measurements."""
    text, pages = '', 0
    vector_pages = []
    try:
        import fitz
        doc = fitz.open(stream=data, filetype='pdf'); pages = len(doc)
        text = '\n'.join(page.get_text('text') for page in doc)
        for page_no, page in enumerate(doc, start=1):
            drawings = page.get_drawings()
            rects = [d.get('rect') for d in drawings if d.get('rect')]
            # Render a lightweight page preview and detect yellow/orange route pixels.
            # This is a visual aid only; pixel detection does not determine measured length.
            preview_png = None
            route_overlay_b64 = None
            route_pixel_info = {'liczba_pikseli': 0, 'obwiednia_px': None, 'udział_procentowy': 0.0}
            try:
                import base64
                from PIL import Image
                pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0), alpha=False)
                image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                image.thumbnail((1400, 1400))
                bio = io.BytesIO()
                image.save(bio, format='JPEG', quality=78, optimize=True)
                preview_png = base64.b64encode(bio.getvalue()).decode('ascii')
                # Prefer PDF vector paths in the overlay: raster color thresholds miss pale yellow lines.
                # Highlight yellow/orange vector strokes directly; this remains a visual candidate mask.
                from PIL import ImageDraw
                rgb = image.convert('RGB')
                mask = Image.new('RGBA', rgb.size, (0, 0, 0, 0))
                draw_mask = ImageDraw.Draw(mask)
                sx = rgb.width / max(1.0, float(page.rect.width))
                sy = rgb.height / max(1.0, float(page.rect.height))
                vector_candidates = 0
                candidate_bounds = []
                candidate_segments = []
                def _pdf_xy(point):
                    # get_drawings() coordinates are in unrotated page space; page render respects rotation.
                    q = point * page.rotation_matrix
                    return (q.x * sx, q.y * sy)
                def _pdf_pt(point):
                    q = point * page.rotation_matrix
                    return (float(q.x), float(q.y))
                def _add_candidate_segment(a, b):
                    ax, ay = _pdf_pt(a); bx, by = _pdf_pt(b)
                    seg_len = ((bx-ax)**2 + (by-ay)**2) ** 0.5
                    if seg_len < 20:
                        return
                    orient = ('pozioma' if abs(by-ay) <= max(1.5, seg_len*0.015) else
                              ('pionowa' if abs(bx-ax) <= max(1.5, seg_len*0.015) else 'ukośna'))
                    candidate_segments.append({'x1_pkt': round(ax, 2), 'y1_pkt': round(ay, 2),
                        'x2_pkt': round(bx, 2), 'y2_pkt': round(by, 2),
                        'długość_pkt': round(seg_len, 2), 'orientacja': orient,
                        'źródło': 'kolorowy wektor PDF'})
                for path in drawings:
                    color = path.get('color')
                    if not color or len(color) < 3:
                        continue
                    rr, gg, bb = [float(v) for v in color[:3]]
                    is_route_color = (rr >= 0.92 and gg >= 0.78 and bb <= 0.22) or (rr >= 0.92 and 0.30 <= gg <= 0.68 and bb <= 0.12)
                    if not is_route_color:
                        continue
                    rect = path.get('rect')
                    if rect is None or (rect.width < 8 and rect.height < 8):
                        continue
                    vector_candidates += 1
                    corners = [fitz.Point(rect.x0,rect.y0), fitz.Point(rect.x1,rect.y0), fitz.Point(rect.x1,rect.y1), fitz.Point(rect.x0,rect.y1)]
                    candidate_bounds.append([_pdf_xy(q) for q in corners])
                    for item in path.get('items', []):
                        kind = item[0]
                        try:
                            if kind == 'l':
                                a, b = item[1], item[2]
                                draw_mask.line((*_pdf_xy(a), *_pdf_xy(b)), fill=(255, 0, 130, 210), width=3)
                                _add_candidate_segment(a, b)
                            elif kind == 're':
                                r = item[1]
                                corners = [fitz.Point(r.x0,r.y0), fitz.Point(r.x1,r.y0), fitz.Point(r.x1,r.y1), fitz.Point(r.x0,r.y1)]
                                pts = [_pdf_xy(p) for p in corners]
                                draw_mask.line(pts+[pts[0]], fill=(255, 0, 130, 210), width=2)
                                for i in range(4): _add_candidate_segment(corners[i], corners[(i+1)%4])
                            elif kind == 'qu':
                                q = item[1]
                                corners = [q.ul, q.ur, q.lr, q.ll]
                                pts = [_pdf_xy(p) for p in corners]
                                draw_mask.line(pts+[pts[0]], fill=(255, 0, 130, 210), width=2)
                                for i in range(4): _add_candidate_segment(corners[i], corners[(i+1)%4])
                            elif kind == 'c':
                                p0, p1, p2, p3 = item[1:]
                                sampled=[]
                                for ti in range(21):
                                    t=ti/20; u=1-t
                                    x=u**3*p0.x+3*u*u*t*p1.x+3*u*t*t*p2.x+t**3*p3.x
                                    y=u**3*p0.y+3*u*u*t*p1.y+3*u*t*t*p2.y+t**3*p3.y
                                    sampled.append(_pdf_xy(fitz.Point(x,y)))
                                draw_mask.line(sampled, fill=(255, 0, 130, 210), width=2)
                        except Exception:
                            continue
                overlay = Image.alpha_composite(rgb.convert('RGBA'), mask).convert('RGB')
                obio = io.BytesIO(); overlay.save(obio, format='JPEG', quality=86, optimize=True)
                route_overlay_b64 = base64.b64encode(obio.getvalue()).decode('ascii')
                route_pixel_info = {'liczba_pikseli': vector_candidates, 'udział_procentowy': round(100 * vector_candidates / max(1, len(drawings)), 3),
                    'obwiednia_px': ({'x0': round(min(pt[0] for r in candidate_bounds for pt in r)), 'y0': round(min(pt[1] for r in candidate_bounds for pt in r)),
                                      'x1': round(max(pt[0] for r in candidate_bounds for pt in r)), 'y1': round(max(pt[1] for r in candidate_bounds for pt in r))} if candidate_bounds else None),
                    'tryb': 'wektorowy', 'opis': 'Liczba kolorowych elementów wektorowych, nie pikseli ani metrów'}
            except Exception:
                pass
            rects = [d.get('rect') for d in drawings if d.get('rect')]
            colors = Counter()
            for d in drawings:
                color = d.get('color') or d.get('fill')
                if color:
                    try: colors[','.join(str(round(float(v), 2)) for v in color)] += 1
                    except Exception: pass
            bbox = None
            if rects:
                bbox = {'x0': round(min(r.x0 for r in rects), 2), 'y0': round(min(r.y0 for r in rects), 2),
                        'x1': round(max(r.x1 for r in rects), 2), 'y1': round(max(r.y1 for r in rects), 2)}
            page_text = page.get_text('text')
            route_labels = sorted({m.group(0).upper() for m in re.finditer(r'\b(?:CF\s*\d{2,3}\s*/\s*\d{2,3}\s*[-–]\s*[A-Z]{2,4}|H\s*=\s*[+]?\d+(?:[,.]\d+)?\s*M)\b', page_text, re.I)})
            vector_pages.append({'strona': page_no, 'szerokość_pkt': round(page.rect.width, 2), 'wysokość_pkt': round(page.rect.height, 2),
                                 'rysunki_wektorowe': len(drawings), 'obwiednia_wektorów_pkt': bbox,
                                 'kolory_wektorów_top': [{'kolor': k, 'ilość': v} for k,v in colors.most_common(8)],
                                 'etykiety_koryt_i_wysokości': route_labels[:80],
                                 'podgląd_jpeg_base64': preview_png, 'nakładka_kandydatów_tras_base64': route_overlay_b64,
                                 'kandydaci_żółtych_tras': route_pixel_info,
                                 'odcinki_kandydackie': sorted(candidate_segments, key=lambda x: x['długość_pkt'], reverse=True)[:250],
                                 'wstępne_osie_tras': consolidate_pdf_route_segments(candidate_segments)[:250]})
    except Exception:
        try:
            from pypdf import PdfReader
            doc = PdfReader(io.BytesIO(data)); pages = len(doc.pages)
            text = '\n'.join(p.extract_text() or '' for p in doc.pages)
        except Exception as exc:
            return {'plik': name, 'format': 'PDF', 'strony': 0, 'ostrzeżenie': f'Nie udało się odczytać tekstu PDF: {exc}', 'próbki_tekstu': [], 'wzorce': {}}
    lines = [re.sub(r'\s+', ' ', x).strip() for x in text.splitlines() if x.strip()]
    scale = '1:100' if re.search(r'\b1\s*:\s*100\b', text) else None
    return {'plik': name, 'format': 'PDF', 'strony': pages, 'tekst_dostępny': bool(text.strip()),
            'skala_rozpoznana': scale, 'diagnostyka_wektorowa': vector_pages,
            'próbki_tekstu': lines[:120], 'wzorce': {'oznaczenia_obwodów': sorted({x.strip() for x in CIRCUIT_RE.findall(text) if not re.fullmatch(r'\d+(?:[.,]\d+)?', x.strip())})[:100], 'typy_kabli': sorted(set(m.group(0).upper() for m in CABLE_RE.finditer(text)))}}


def build_profile(project_name: str, analyses: list[dict], route_layers: list[str], device_layers: list[str], unit: str, room_height: float, reserve_pct: float) -> dict:
    return {'schema_version': 1, 'nazwa_projektu': project_name, 'źródła': [a.get('plik') for a in analyses],
            'jednostki_CAD': unit, 'warstwy_tras_kablowych': route_layers, 'warstwy_urządzeń': device_layers,
            'parametry_pomiaru': {'wysokość_pomieszczenia_m': room_height, 'zapas_kabla_proc': reserve_pct},
            'wzorce': {a.get('plik','źródło'): a.get('wzorce', {}) for a in analyses},
            'zasady': {'zakres_CAD': 'wyłącznie Model Space', 'legenda': 'wykluczać po potwierdzeniu', 'wspólne_trasy': 'mierzyć raz; długości kabli liczyć osobno dla obwodów', 'pewność': 'sugestie wymagają zatwierdzenia użytkownika'}}
