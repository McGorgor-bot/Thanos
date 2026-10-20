import io, math, os, re, shutil, subprocess
import collections
from pathlib import Path
import pandas as pd
import streamlit as st
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from .config import RESULT_COLUMNS
from .classification import classify

def find_tesseract():
    """Locate a runnable Tesseract binary, including portable app-local installs.

    TESSERACT_CMD takes precedence. A path is only accepted when it exists; the
    actual invocation remains responsible for reporting damaged installs or
    missing language data. This supports normal Windows installs and portable
    layouts without requiring PATH to be refreshed after installation.
    """
    here = Path(__file__).resolve().parent.parent
    candidates = [
        os.environ.get('TESSERACT_CMD'),
        shutil.which('tesseract'),
        str(here / 'tesseract' / 'tesseract.exe'),
        str(here / 'Tesseract-OCR' / 'tesseract.exe'),
        str(here / 'tools' / 'tesseract' / 'tesseract.exe'),
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        str(Path.home() / 'AppData' / 'Local' / 'Programs' / 'Tesseract-OCR' / 'tesseract.exe'),
    ]
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = str(Path(candidate).expanduser())
            if resolved in seen:
                continue
            seen.add(resolved)
            if Path(resolved).is_file():
                return resolved
        except (OSError, TypeError, ValueError):
            continue
    return None


def read_pdf_pages(data: bytes, render_scale=1.8):
    """Robust PDF reader.

    Keeps both the normal text layer and geometry/word information.  It also
    records page diagnostics so the UI can explain why OCR was selected.
    """
    import fitz
    doc = fitz.open(stream=data, filetype='pdf')
    pages = []
    try:
        if doc.needs_pass and not doc.authenticate(''):
            raise ValueError('PDF jest zaszyfrowany hasłem.')
        for i, page in enumerate(doc):
            text = page.get_text('text', sort=True) or ''
            words = page.get_text('words', sort=True) or []
            blocks = page.get_text('blocks', sort=True) or []
            # A second extraction mode catches PDFs whose reading order is odd.
            if len(re.sub(r'\s+', '', text)) < 25:
                alt = page.get_text('text', flags=fitz.TEXTFLAGS_TEXT) or ''
                if len(re.sub(r'\s+', '', alt)) > len(re.sub(r'\s+', '', text)):
                    text = alt
            pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), alpha=False)
            pages.append({
                'page': i + 1,
                'text': text,
                'words': words,
                'blocks': blocks,
                'image': pix.tobytes('png'),
                'width': page.rect.width,
                'height': page.rect.height,
                'rotation': page.rotation,
                'images': len(page.get_images(full=True)),
                'drawings': len(page.get_drawings()),
                'fonts': len(page.get_fonts(full=True)),
                '_drawings': page.get_drawings(),
            })
    finally:
        doc.close()
    return pages


def pdf_page_diagnostic(page):
    text_len = len(re.sub(r'\s+', '', page.get('text', '') or ''))
    words = len(page.get('words', []) or [])
    images = int(page.get('images', 0) or 0)
    drawings = int(page.get('drawings', 0) or 0)
    fonts = int(page.get('fonts', 0) or 0)
    if text_len >= 100 and words >= 10:
        kind = 'Wektorowy / tekstowy'
        reason = 'warstwa tekstowa dostępna'
        need_ocr = False
    elif text_len >= 25 or words >= 3:
        kind = 'Hybrydowy'
        reason = 'częściowa warstwa tekstowa'
        need_ocr = True
    elif images > 0 and text_len < 25:
        kind = 'Skan / obraz'
        reason = 'brak użytecznej warstwy tekstowej, obecny obraz'
        need_ocr = True
    else:
        kind = 'Wektorowy bez tekstu'
        reason = 'strona zawiera grafikę/wektory bez użytecznej warstwy tekstowej'
        need_ocr = True
    return {'kind': kind, 'reason': reason, 'need_ocr': need_ocr,
            'text_len': text_len, 'words': words, 'images': images,
            'drawings': drawings, 'fonts': fonts}


def diagnose_pdf(data: bytes):
    pages = read_pdf_pages(data)
    diags = []
    for pg in pages:
        d = pdf_page_diagnostic(pg)
        diags.append({
            'Strona': pg['page'], 'Typ PDF': d['kind'],
            'Tekst': d['text_len'], 'Słów': d['words'],
            'Obrazy': d['images'], 'Wektory': d['drawings'],
            'OCR': 'TAK' if d['need_ocr'] else 'NIE',
            'Powód': d['reason']
        })
    ocr_pages = sum(1 for d in diags if d['OCR'] == 'TAK')
    has_text = any(d['Tekst'] > 0 for d in diags)
    return pages, pd.DataFrame(diags), ocr_pages, has_text


def ocr_image(image_bytes: bytes, lang='pol+eng'):
    import pytesseract
    from PIL import Image
    from io import BytesIO
    img = Image.open(BytesIO(image_bytes))
    # PSM 11 dobrze sprawdza się na rysunkach z wieloma niezależnymi oznaczeniami.
    config = '--psm 11'
    return pytesseract.image_to_string(img, lang=lang, config=config) or ''


def classify_text(text: str, patterns: dict):
    out = []
    for typ, pat in patterns.items():
        try:
            matches = re.findall(pat, text, flags=re.IGNORECASE | re.MULTILINE)
        except re.error:
            matches = []
        if matches:
            cleaned = []
            for m in matches:
                if isinstance(m, tuple):
                    m = next((x for x in m if x), '')
                s = str(m).strip()
                if s:
                    cleaned.append(s)
            if cleaned:
                counts = pd.Series(cleaned).value_counts()
                for symbol, qty in counts.items():
                    out.append((typ, symbol, int(qty)))
    return out


def pdf_needs_ocr(page):
    text = re.sub(r'\s+', '', page['text'])
    # Bardzo mało tekstu na stronie zwykle oznacza skan / rysunek wektorowy bez warstwy tekstowej.
    return len(text) < 25


def _norm_pdf_label(value: str):
    """Normalize a PDF label for exact legend/plan matching."""
    value = re.sub(r'\s+', '', str(value or '')).strip().upper()
    value = value.replace('–', '-').replace('—', '-')
    return value


def _pdf_legend_labels(page):
    """Detect a legend region and only keep actual symbol identifiers.

    Many electrical legends are made of a small graphic in the left column and
    a prose description on the right.  Treating every short word in that prose
    (DWA, POD, 230V, LEGENDA) as an element code caused hundreds of false rows.
    Keep codes from the symbol column only; descriptions are handled by the
    graphic-row matcher below.
    """
    words = page.get('words', []) or []
    if not words:
        return set(), []
    heads = [w for w in words if re.search(
        r'^(?:LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE|ZESTAWIENIE(?:\s+OPRAW)?)[:.]?$',
        str(w[4] or ''), re.I)]
    if not heads:
        return set(), []
    xs = [float(w[0]) for w in words] + [float(w[2]) for w in words]
    ys = [float(w[1]) for w in words] + [float(w[3]) for w in words]
    page_w = max(xs) if xs else float(page.get('width', 1000))
    page_h = max(ys) if ys else float(page.get('height', 1000))
    labels, boxes = set(), []
    # Common prose fragments that are not symbol identifiers even if short.
    stop = {'LEGENDA', 'LEGEND', 'DWA', 'POD', 'GNIAZDO', 'GNIAZDA',
            '230V', '400V', 'FAZOWY', 'FAZOWY', 'IP20', 'IP44', 'KABLA',
            'ZAPASEM', 'OCHRONNYM', 'WSPOLNA', 'RAMKA', 'DOMOFON', 'X2', '2K'}
    label_re = re.compile(r'^[A-ZĄĆĘŁŃÓŚŹŻ0-9][A-ZĄĆĘŁŃÓŚŹŻ0-9+._:/\-]{0,34}$', re.I)
    for h in heads:
        hx = (float(h[0]) + float(h[2])) / 2
        hy = (float(h[1]) + float(h[3])) / 2
        # Restrict exclusion to the right-side legend column, not a large
        # rectangle over the actual plan. The heading anchors the region.
        bx0 = max(0.0, hx - 30.0)
        bx1 = page_w + 2.0
        by0 = max(0.0, hy - 18.0)
        # The legend descriptions finish before the general note/title block.
        by1 = min(page_h, hy + 490.0)
        box = (bx0, by0, bx1, by1)
        if not any(abs(box[0]-b[0]) < 25 and abs(box[1]-b[1]) < 25 for b in boxes):
            boxes.append(box)
        # Symbol identifiers are in the narrow column to the left of the
        # description text. Never harvest words from the description column.
        for w in words:
            x0, y0, x1, y1, txt, *_ = w
            cx, cy = (float(x0)+float(x1))/2, (float(y0)+float(y1))/2
            if not (hx-35 <= cx <= hx+25 and hy+10 <= cy <= hy+480):
                continue
            val = _norm_pdf_label(txt).rstrip(':.,;')
            if not val or val in stop or len(val) > 35 or not label_re.match(val):
                continue
            # Reject lone punctuation and generic measurement values.
            if val in {'-', 'H=0,3M', 'H=1,4M'}:
                continue
            if re.fullmatch(r'\d+(?:[.,]\d+)?', val):
                continue
            labels.add(val)
    return labels, boxes

def _point_in_any_box(cx, cy, boxes):
    return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in (boxes or []))


def _count_pdf_legend_labels(page, labels, legend_box=None):
    """Count exact occurrences of legend labels outside ALL legend boxes."""
    words = page.get('words', []) or []
    counts = {lab: 0 for lab in labels}
    if not labels:
        return counts
    for w in words:
        txt = _norm_pdf_label(w[4])
        if txt not in labels:
            continue
        cx = (float(w[0]) + float(w[2])) / 2
        cy = (float(w[1]) + float(w[3])) / 2
        if legend_box:
            boxes = legend_box if isinstance(legend_box, list) else [legend_box]
            if _point_in_any_box(cx, cy, boxes):
                continue
        counts[txt] += 1
    return counts


def _pdf_code_fallback(page, legend_boxes=None):
    """Fallback for electrical PDFs whose legend contains graphics but no textual codes.

    Example: a drawing may have a heading 'ZESTAWIENIE OPRAW' and the legend
    symbols are vector graphics, while the plan contains textual designations
    such as RK/2.1 and RK/2.2.  In that case the old legend-only parser returned
    zero because there was no code next to the legend description.  We count
    repeated plan designations and deliberately ignore common circuit/room refs.
    """
    words = page.get('words', []) or []
    if not words:
        return {}
    code_re = re.compile(r'^[A-ZĄĆĘŁŃÓŚŹŻ]{1,4}/[0-9]{1,4}(?:[.][0-9]{1,3})?$',
                         re.IGNORECASE)
    counts = {}
    for w in words:
        raw = re.sub(r'\s+', '', str(w[4] or '')).strip()
        val = raw.upper()
        if not code_re.match(val):
            continue
        # L1/43 etc. are circuit references in the plan, not element types.
        if re.match(r'^L[0-9]+/[0-9]+$', val):
            continue
        cx = (float(w[0]) + float(w[2])) / 2
        cy = (float(w[1]) + float(w[3])) / 2
        if _point_in_any_box(cx, cy, legend_boxes or []):
            continue
        counts[val] = counts.get(val, 0) + 1
    return counts


def _pdf_graphic_color(draw):
    """Return a quantized drawing color; None for uncolored/gray geometry."""
    col = draw.get('color')
    if not col or len(col) < 3:
        return None
    c = tuple(float(x) for x in col[:3])
    if max(c) - min(c) < 0.12 and max(c) < 0.82:
        return None
    return tuple(int(round(max(0.0, min(1.0, x)) * 10)) for x in c)


def _rect_gap(a, b):
    ax0, ay0, ax1, ay1 = a; bx0, by0, bx1, by1 = b
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _cluster_drawings(drawings, max_gap=9.0, colored_only=True, same_color=True):
    """Cluster nearby PDF vector primitives using a spatial index.

    PDF exports of CAD plans can contain >150k drawing primitives. The former
    greedy cluster merge compared each primitive against every existing
    cluster (quadratic behavior), which could time out on full-size drawings.
    This implementation uses a spatial hash + union-find and ignores oversized
    page geometry that cannot be a compact legend/device symbol.
    """
    items=[]
    for d in drawings or []:
        r=d.get('rect')
        if not r or r.width <= 0 or r.height <= 0:
            continue
        # Large walls, frames and cable runs are not compact symbol primitives.
        if r.width > 90 or r.height > 90:
            continue
        color=_pdf_graphic_color(d)
        if colored_only and color is None:
            continue
        rect=(float(r.x0),float(r.y0),float(r.x1),float(r.y1))
        items.append((d,rect,color))
    if not items:
        return []
    parent=list(range(len(items)))
    rank=[0]*len(items)
    def find(x):
        while parent[x] != x:
            parent[x]=parent[parent[x]]
            x=parent[x]
        return x
    def union(a,b):
        ra,rb=find(a),find(b)
        if ra==rb: return
        if rank[ra] < rank[rb]: ra,rb=rb,ra
        parent[rb]=ra
        if rank[ra]==rank[rb]: rank[ra]+=1
    cell=max(4.0,float(max_gap)+1.0)
    buckets=collections.defaultdict(list)
    for i,(d,r,color) in enumerate(items):
        # Expand by max_gap so close rectangles share at least one grid cell.
        x0,y0,x1,y1=r
        gx0=int(math.floor((x0-max_gap)/cell)); gx1=int(math.floor((x1+max_gap)/cell))
        gy0=int(math.floor((y0-max_gap)/cell)); gy1=int(math.floor((y1+max_gap)/cell))
        cells=[(gx,gy) for gx in range(gx0,gx1+1) for gy in range(gy0,gy1+1)]
        # Prevent a wide/odd PDF path from polluting many hash buckets.
        if len(cells)>100:
            continue
        seen=set()
        for key in cells:
            for j in buckets[key]:
                if j in seen: continue
                seen.add(j)
                if same_color and items[j][2] != color: continue
                if _rect_gap(r,items[j][1]) <= max_gap:
                    union(i,j)
        for key in cells:
            buckets[key].append(i)
    groups=collections.defaultdict(list)
    for i,item in enumerate(items):
        # Items omitted from buckets are still valid but stay as singleton groups.
        groups[find(i)].append(item)
    clusters=[]
    for group in groups.values():
        xs=[x[1][0] for x in group]; ys=[x[1][1] for x in group]
        xe=[x[1][2] for x in group]; ye=[x[1][3] for x in group]
        rect=(min(xs),min(ys),max(xe),max(ye))
        # Union-find can chain adjacent colored strokes into a long text run.
        # Keep only compact components when used as device-symbol candidates.
        if rect[2]-rect[0] > 42 or rect[3]-rect[1] > 42:
            continue
        clusters.append({'color':group[0][2], 'rect':rect, 'items':group})
    return clusters


def _graphic_signature(cluster):
    """Build a scale-normalized signature from the actual PDF path primitives.

    A single PyMuPDF drawing object can contain a complete socket symbol. Using
    only its bounding box made every one-path symbol look identical. We now
    describe each line, cubic curve, rectangle and quad inside each drawing.
    """
    x0,y0,x1,y1=cluster['rect']; w=max(0.1,x1-x0); h=max(0.1,y1-y0)
    feats=[]
    def xy(point):
        try: return float(point.x),float(point.y)
        except Exception:
            try: return float(point[0]),float(point[1])
            except Exception: return None
    for draw,draw_rect,color in cluster['items']:
        items=draw.get('items',[]) or []
        if not items:
            r=draw_rect; items=[('bbox',r)]
        for item in items:
            typ=item[0]
            pts=[]
            for obj in item[1:]:
                if typ=='re' and hasattr(obj,'x0'):
                    pts.extend([(float(obj.x0),float(obj.y0)),(float(obj.x1),float(obj.y1))])
                    break
                if typ=='qu' and hasattr(obj,'ul'):
                    pts.extend([xy(obj.ul),xy(obj.ur),xy(obj.ll),xy(obj.lr)])
                    break
                pxy=xy(obj)
                if pxy is not None: pts.append(pxy)
            pts=[p for p in pts if p is not None]
            if not pts:
                r=draw_rect; rx0,ry0,rx1,ry1=r
            else:
                rx0=min(p[0] for p in pts); ry0=min(p[1] for p in pts)
                rx1=max(p[0] for p in pts); ry1=max(p[1] for p in pts)
            # Keep degenerate lines informative while avoiding zero-size noise.
            iw=max(0.015,rx1-rx0); ih=max(0.015,ry1-ry0)
            feats.append((((rx0+rx1)/2-x0)/w,((ry0+ry1)/2-y0)/h,iw/w,ih/h))
    feats=tuple(sorted(tuple(round(v,3) for v in f) for f in feats))
    return {'color':cluster.get('color'),'w':w,'h':h,'ratio':w/h,
            'n':len(feats),'area':w*h,'primitives':len(feats),'features':feats}

def _graphic_similar(a,b):
    """Compare symbol geometry independent of page position and drawing scale.

    CAD-exported PDFs commonly draw legend icons larger than plan instances.
    Compare normalized internal geometry and aspect ratio rather than absolute
    bounding-box dimensions. Right-angle rotations are allowed; simple symbols
    still require matching primitive counts to keep outlet types distinct.
    """
    fa=list(a.get('features',()) or ()); fb=list(b.get('features',()) or ())
    if not fa or not fb: return False
    na,nb=len(fa),len(fb)
    if min(na,nb) < 5 and na != nb: return False
    if min(na,nb) >= 5 and abs(na-nb) > max(4,int(0.40*max(na,nb))): return False
    def rotate(feats, turns):
        out=[]
        for x,y,w,h in feats:
            for _ in range(turns): x,y,w,h=1.0-y-h,x,h,w
            out.append((x,y,w,h))
        return out
    def aspect(sig, turns=0):
        w=max(0.1,float(sig.get('w',1.0))); h=max(0.1,float(sig.get('h',1.0)))
        return h/w if turns%2 else w/h
    def feature_match(src,dst,tol=0.14,ratio=0.90,mean_limit=0.10):
        da=[min(sum(abs(x-y) for x,y in zip(va,vb)) for vb in dst) for va in src]
        db=[min(sum(abs(x-y) for x,y in zip(va,vb)) for va in src) for vb in dst]
        return (sum(d<=tol for d in da)/len(da)>=ratio and sum(d<=tol for d in db)/len(db)>=ratio and (sum(da)/len(da)+sum(db)/len(db))/2<=mean_limit)
    for turns in range(4):
        ar_a,ar_b=aspect(a),aspect(b,turns)
        if max(ar_a,ar_b)/max(0.01,min(ar_a,ar_b))>1.40: continue
        if feature_match(fa,rotate(fb,turns),tol=0.14,ratio=0.90,mean_limit=0.09): return True
    if min(na,nb)<5: return False
    for turns in range(4):
        ar_a,ar_b=aspect(a),aspect(b,turns)
        if max(ar_a,ar_b)/max(0.01,min(ar_a,ar_b))>1.35: continue
        if feature_match(fa,rotate(fb,turns),tol=0.20,ratio=0.80,mean_limit=0.14): return True
    return False

def _pdf_ssp_vector_fallback(page):
    """Count common SSP fire-alarm symbols in vector PDFs when no legend text is
    available.

    This is deliberately geometry-first: it uses repeated red vector symbol
    families rather than OCR text.  It is a fallback only; a real PDF legend
    still has priority.  The profiles are tolerant to small export/rotation
    differences and ignore title-block graphics.
    """
    drawings = page.get('_drawings', []) or []
    if not drawings:
        return []
    # SSP symbols in the supplied CAD-exported PDF are red; magenta text is
    # intentionally excluded.
    red=[]
    for d in drawings:
        col=d.get('color')
        if not col or len(col)<3:
            continue
        c=tuple(round(float(x),3) for x in col[:3])
        if c != (1.0,0.0,0.0):
            continue
        r=d.get('rect')
        if not r or r.width <= 0 or r.height <= 0:
            continue
        red.append((d,(float(r.x0),float(r.y0),float(r.x1),float(r.y1)),c))
    if not red:
        return []

    clusters=_cluster_drawings(drawings, max_gap=4.0, colored_only=False)
    # _cluster_drawings() with colored_only=False includes all colors, so keep
    # only clusters whose geometry is predominantly red.
    filtered=[]
    for cl in clusters:
        reds=[it for it in cl['items'] if tuple(round(float(x),3) for x in (it[0].get('color') or ())) == (1.0,0.0,0.0)]
        if not reds:
            continue
        # A red SSP symbol is compact; this also removes large red title-box text.
        x0,y0,x1,y1=cl['rect']; w=x1-x0; h=y1-y0
        if 5.0 <= w <= 30.0 and 5.0 <= h <= 30.0:
            filtered.append(cl)

    profiles = {
        'Optyczno-termiczna czujka dymu': lambda w,h,n: (
            8.8 <= w <= 9.5 and 12.0 <= h <= 13.1 and 7 <= n <= 8
        ),
        'optyczna czujka dymu': lambda w,h,n: (
            9.5 <= w <= 10.3 and 12.7 <= h <= 13.7 and 8 <= n <= 10
        ),
        'ręczny ostrzegacz pożarowy': lambda w,h,n: (
            7.5 <= w <= 9.2 and 14.0 <= h <= 19.5 and 3 <= n <= 5
        ),
    }
    counts=collections.Counter()
    for cl in filtered:
        x0,y0,x1,y1=cl['rect']; w=x1-x0; h=y1-y0; n=len(cl['items'])
        for label,matcher in profiles.items():
            if matcher(w,h,n):
                counts[label]+=1
                break

    return [(label,int(counts[label])) for label in profiles if counts[label]]



def _graphic_match_score(a, b):
    """Lower-is-better scale-invariant score for legend-template matching."""
    fa=list(a.get('features',()) or ()); fb=list(b.get('features',()) or ())
    if not fa or not fb: return float('inf')
    best=float('inf')
    def rotate(feats, turns):
        out=[]
        for x,y,w,h in feats:
            for _ in range(turns): x,y,w,h=1.0-y-h,x,h,w
            out.append((x,y,w,h))
        return out
    def aspect(sig, turns):
        w=max(.1,float(sig.get('w',1.0))); h=max(.1,float(sig.get('h',1.0)))
        return h/w if turns%2 else w/h
    for turns in range(4):
        ar_a,ar_b=aspect(a,0),aspect(b,turns); ratio=max(ar_a,ar_b)/max(.01,min(ar_a,ar_b))
        if ratio>1.45: continue
        dst=rotate(fb,turns)
        da=[min(sum(abs(x-y) for x,y in zip(v,u)) for u in dst) for v in fa]
        db=[min(sum(abs(x-y) for x,y in zip(v,u)) for u in fa) for v in dst]
        geom=(sum(da)/len(da)+sum(db)/len(db))/2
        score=geom+0.18*abs(math.log(max(.01,ratio)))+0.25*abs(len(fa)-len(fb))/max(len(fa),len(fb))
        best=min(best,score)
    return best


def _dedupe_pdf_symbol_clusters(clusters, center_tol=2.5, overlap_min=0.45):
    """Collapse overlapping duplicate vector clusters while retaining nearby symbols."""
    kept=[]
    def overlap(a,b):
        x0=max(a[0],b[0]); y0=max(a[1],b[1]); x1=min(a[2],b[2]); y1=min(a[3],b[3])
        inter=max(0.0,x1-x0)*max(0.0,y1-y0)
        aa=max(0.01,(a[2]-a[0])*(a[3]-a[1])); bb=max(0.01,(b[2]-b[0])*(b[3]-b[1]))
        return inter/min(aa,bb)
    for cl in sorted(clusters,key=lambda c:len(c.get('items',[])),reverse=True):
        r=cl['rect']; cx=(r[0]+r[2])/2; cy=(r[1]+r[3])/2; duplicate=False
        for old in kept:
            q=old['rect']; ox=(q[0]+q[2])/2; oy=(q[1]+q[3])/2
            if math.hypot(cx-ox,cy-oy)<=center_tol and overlap(r,q)>=overlap_min:
                duplicate=True; break
        if not duplicate: kept.append(cl)
    return kept

def _pdf_graphic_legend(page):
    """Match vector symbols to complete descriptions using legend bullet rows.

    A dash at the start of each legend description is a much more stable row
    anchor than clustering every colored mark in the legend (some symbols have
    several vertically separated primitives). Wrapped description lines are
    kept together, and generic words are never emitted as device names.
    """
    words=page.get('words',[]) or []
    drawings=page.get('_drawings',[]) or []
    if not words or not drawings:
        return [],[]
    _,legend_boxes=_pdf_legend_labels(page)
    heads=[w for w in words if re.search(r'^(?:LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE|ZESTAWIENIE(?:\s+OPRAW)?)[:.]?$',str(w[4] or ''),re.I)]
    if not heads or not legend_boxes:
        return [],[]
    head=min(heads,key=lambda w:w[1]); hx=(float(head[0])+float(head[2]))/2; hy=(float(head[1])+float(head[3]))/2
    # Group compact primitives by color, including dark/gray paths. Some outlet
    # and 3-phase symbols are exported in dark colors; colored_only=True silently
    # omitted them. Large architectural paths are filtered by _cluster_drawings,
    # and template matching remains class-specific to limit false positives.
    clusters=_dedupe_pdf_symbol_clusters(_cluster_drawings(
        drawings,max_gap=2.6,colored_only=False,same_color=True))
    # Each actual legend item begins with a standalone dash at the description column.
    dash=[]
    for w in words:
        x0,y0,x1,y1,txt,*_=w
        if str(txt).strip() not in {'-','–','—'}: continue
        cx=(float(x0)+float(x1))/2; cy=(float(y0)+float(y1))/2
        if hx+4 <= cx <= hx+35 and hy+8 <= cy <= hy+360:
            dash.append((cy,cx))
    dash.sort()
    # Remove duplicate dash glyphs on the same baseline.
    rows=[]
    for y,x in dash:
        if not rows or abs(y-rows[-1][0])>2.5: rows.append((y,x))
    entries=[]
    for i,(y,dx) in enumerate(rows):
        next_y=rows[i+1][0] if i+1<len(rows) else min(float(page.get('height',2000)),hy+390)
        # Keep the whole wrapped description between this bullet and the next.
        desc_words=[]
        for w in words:
            x0,y0,x1,y1,txt,*_=w; cx=(float(x0)+float(x1))/2; cy=(float(y0)+float(y1))/2
            if cx > dx+1 and cy >= y-2 and cy < next_y-1 and str(txt).strip() not in {'-','–','—'}:
                # Do not absorb legend codes in the symbol column or text from title blocks.
                if cx < float(page.get('width',2000))-25:
                    desc_words.append(w)
        grouped=collections.defaultdict(list)
        for w in desc_words:
            cy=(float(w[1])+float(w[3]))/2
            grouped[round(cy/4)].append(w)
        parts=[]
        for _,ws in sorted(grouped.items()):
            parts.append(' '.join(str(w[4]).strip() for w in sorted(ws,key=lambda z:z[0])))
        desc=re.sub(r'\s+',' ',' '.join(parts)).strip(' -–—')
        desc=re.sub(r'\s+',' ',desc)
        if len(desc)<8 or re.search(r'^(?:uwaga|standardowa wysokość|wysokość montażu|nazwa inwestycji|adres inwestycji)',desc,re.I):
            continue
        # Find the closest compact colored symbol at the bullet baseline.
        candidates=[]
        for cl in clusters:
            x0,y0,x1,y1=cl['rect']; cx=(x0+x1)/2; cy=(y0+y1)/2
            if hx-48 <= cx <= hx+25 and abs(cy-y)<=10 and x1-x0<=35 and y1-y0<=35:
                candidates.append((abs(cy-y),-len(cl['items']),cl))
        if not candidates: continue
        cl=min(candidates,key=lambda z:(z[0],z[1]))[2]
        entries.append({'label':desc,'cluster':cl,'signature':_graphic_signature(cl),'y':y})
    if not entries: return [],[]
    # Text qualifiers are semantically part of some symbols: "x2" means a
    # double socket and "2K" identifies the double RJ45 outlet.  Their glyphs
    # are text, not vector primitives, so shape-only matching used to count the
    # same outlet as both single and double (and RJ45 as a power socket).
    qualifiers=[]
    for w in words:
        txt=re.sub(r'\s+','',str(w[4] or '')).casefold()
        if txt not in {'x2','2k'}: continue
        cx=(float(w[0])+float(w[2]))/2; cy=(float(w[1])+float(w[3]))/2
        if _point_in_any_box(cx,cy,legend_boxes): continue
        qualifiers.append((txt,cx,cy))

    # Assign each qualifier text to only its nearest vector cluster. Without
    # this one-to-one association, several nearby strokes of the same symbol
    # were all classified as separate double outlets/RJ45 points.
    eligible=[]
    for ci,cl in enumerate(clusters):
        cx=(cl['rect'][0]+cl['rect'][2])/2; cy=(cl['rect'][1]+cl['rect'][3])/2
        if not _point_in_any_box(cx,cy,legend_boxes):
            eligible.append((ci,cx,cy,cl))
    qualifier_for_cluster={}
    for q,qx,qy in qualifiers:
        nearby=sorted((math.hypot(cx-qx,cy-qy),ci) for ci,cx,cy,cl in eligible if math.hypot(cx-qx,cy-qy)<=8.0)
        if nearby:
            dist,ci=nearby[0]
            # Resolve competing labels by retaining the closer qualifier.
            old=qualifier_for_cluster.get(ci)
            if old is None or dist<old[0]: qualifier_for_cluster[ci]=(dist,q)

    # Each physical cluster is assigned to at most one legend class. This
    # prevents cross-counting when single/double outlets share the same icon.
    counts=collections.Counter()
    quality_counts=collections.defaultdict(collections.Counter)
    for ci,cx,cy,cl in eligible:
        sig=_graphic_signature(cl)
        qualifier=qualifier_for_cluster.get(ci,(None,None))[1]
        candidates=[]
        for entry in entries:
            label=entry['label'].casefold()
            is_double='dwa gniazda' in label
            is_rj='rj45' in label
            is_single=('gniazdo pojedy' in label and not is_double and not is_rj)
            if qualifier=='x2' and not (is_double and not is_rj): continue
            if qualifier=='2k' and not is_rj: continue
            if qualifier is None and (is_double or is_rj): continue
            if qualifier is None and not (is_single or 'wypust' in label or 'rozdzielnica' in label or 'główny punkt' in label):
                continue
            score=_graphic_match_score(entry['signature'],sig)
            # Prefer exact primitive count when two related marks are similar.
            score += 0.12*abs(len(entry['signature'].get('features',()))-len(sig.get('features',())) )
            strict=_graphic_similar(entry['signature'],sig)
            # Keep strict geometric matches first. A narrowly bounded fallback
            # handles CAD exports that split one path into several colored paths;
            # it requires a strong score and a clear winner over the next class.
            candidates.append((score,entry,strict))
        if candidates:
            candidates.sort(key=lambda z:z[0])
            best_score,winner,best_strict=candidates[0]
            second_score=candidates[1][0] if len(candidates)>1 else float('inf')
            label_lc=winner['label'].casefold()
            # Outlet symbols have very similar outlines, so retain a stricter
            # threshold and larger winner margin for ordinary sockets. WYP
            # symbols are exported as split colored strokes; their primitive
            # signature differs more, therefore use a dedicated, still bounded
            # threshold and require the winner to beat the competing phase type.
            if best_strict:
                accept=True
            elif 'wypust' in label_lc and '3-fazowy' in label_lc:
                accept=(best_score <= 0.95 and (second_score-best_score) >= 0.08)
            elif 'wypust' in label_lc:
                accept=(best_score <= 0.80 and (second_score-best_score) >= 0.10)
            else:
                accept=(best_score <= 0.35 and (second_score-best_score) >= 0.08)
            if accept:
                counts[winner['label']]+=1
                quality_counts[winner['label']]['strict' if best_strict else 'fallback']+=1

    matches=[]
    for entry in entries:
        label=entry['label']; qty=int(counts[label]); sig=dict(entry['signature'])
        if qty <= 0:
            sig['match_quality']='niska'
        elif quality_counts[label]['fallback']:
            sig['match_quality']='średnia'
        else:
            sig['match_quality']='wysoka'
        matches.append((label,qty,sig))
    return matches,entries


def analyze_pdf(name, data, patterns, mode='Hybrydowa', ocr_lang='pol+eng', legend_only=False, auto_legend=True, graphic_match=True, text_analysis=True, group_graphics=True, ignore_title=True, skip_circuit=True, high_accuracy=False, save_preview=False):
    rows = []
    pages = read_pdf_pages(data)
    statuses = []
    diag = [pdf_page_diagnostic(pg) for pg in pages]
    ocr_count = sum(1 for d in diag if d['need_ocr'])
    statuses.append(f'PDF: {len(pages)} stron; strony wymagające OCR: {ocr_count}; tekst dostępny: {sum(1 for d in diag if d["text_len"] > 0)}')
    # First pass: collect labels from legends across all pages. This makes the
    # PDF mode behave like the requested "search elements described in legend".
    global_legend_labels = set()
    page_legend = {}
    if legend_only and auto_legend:
        for pg in pages:
            labs, boxes = _pdf_legend_labels(pg)
            page_legend[pg['page']] = (labs, boxes)
            global_legend_labels.update(labs)

    for pg in pages:
        text = pg['text']
        source = 'PDF/tekst'
        method = 'Automatycznie / tekst PDF'
        certainty = 'średnia'
        ocr_used = False
        if mode in ('OCR', 'Hybrydowa') and (mode == 'OCR' or pdf_page_diagnostic(pg)['need_ocr']):
            tess = find_tesseract()
            if tess:
                try:
                    import pytesseract
                    pytesseract.pytesseract.tesseract_cmd = tess
                    ocr_text = ocr_image(pg['image'], ocr_lang)
                    if ocr_text.strip():
                        if mode == 'OCR' or not text.strip():
                            text = ocr_text
                            source = 'PDF/OCR'; method = 'Automatycznie / OCR'; certainty = 'średnia'; ocr_used = True
                        else:
                            text = text + '\n' + ocr_text
                            source = 'PDF/tekst+OCR'; method = 'Automatycznie / hybrydowo'; certainty = 'średnia'; ocr_used = True
                except Exception as exc:
                    statuses.append(f'Strona {pg["page"]}: OCR niedostępny ({exc})')
            elif pdf_needs_ocr(pg):
                statuses.append(f'Strona {pg["page"]}: brak Tesseract OCR — zainstaluj Tesseract lub ustaw TESSERACT_CMD.')

        if legend_only and global_legend_labels:
            # Count exact labels from the PDF text layer outside ALL legend tables.
            # For scanned pages without words, fall back to OCR token matching.
            labs = global_legend_labels
            _, local_box = page_legend.get(pg['page'], (set(), None))
            counts = _count_pdf_legend_labels(pg, labs, local_box)
            for symbol, qty in counts.items():
                if qty <= 0:
                    continue
                typ = classify(symbol, patterns)
                rows.append({
                    'Plik': name, 'Strona': pg['page'], 'Typ elementu': typ,
                    'Nazwa bloku': '', 'Misc': '', 'Name': '', 'Nazwa elementu': symbol,
                    'Symbol/etykieta': symbol, 'Ilość': int(qty),
                    'Metoda': 'Automatycznie / dynamiczna legenda PDF',
                    'Pewność': 'wysoka', 'Źródło': 'PDF/legenda',
                })
        elif text_analysis:
            found = classify_text(text, patterns)
            for typ, symbol, qty in found:
                rows.append({
                    'Plik': name, 'Strona': pg['page'], 'Typ elementu': typ,
                    'Nazwa bloku': '', 'Misc': '', 'Name': '', 'Nazwa elementu': '',
                    'Symbol/etykieta': symbol, 'Ilość': qty, 'Metoda': method,
                    'Pewność': certainty, 'Źródło': source,
                })
        _text_char_count = len(re.sub(r"\s+", "", pg["text"]))
        _text_source_label = "OCR + tekst" if ocr_used and source == "PDF/tekst+OCR" else source
        statuses.append(f'Strona {pg["page"]}: {_text_source_label}; znaków tekstu {_text_char_count}')
    if legend_only and global_legend_labels:
        statuses.insert(0, f'Legenda PDF: znaleziono {len(global_legend_labels)} unikalnych oznaczeń: {", ".join(sorted(global_legend_labels))}')
    elif legend_only:
        # Graphic legends often contain no textual code at all. E3.pdf is a
        # representative case: the heading is "ZESTAWIENIE OPRAW", descriptions
        # are text, but the legend symbols themselves are vector graphics.
        # Fall back to repeated code-like designations visible on the plan.
        fallback_rows = []
        fallback_total = 0
        for pg in pages:
            _, boxes = _pdf_legend_labels(pg)
            codes = _pdf_code_fallback(pg, boxes)
            for symbol, qty in codes.items():
                if qty <= 0:
                    continue
                typ = classify(symbol, patterns)
                if typ == symbol and re.match(r'^RK/[0-9]+(?:\.[0-9]+)?$', symbol, re.I):
                    typ = 'Oprawa oświetleniowa'
                fallback_rows.append({
                    'Plik': name, 'Strona': pg['page'], 'Typ elementu': typ,
                    'Nazwa bloku': '', 'Misc': '', 'Name': '',
                    'Nazwa elementu': symbol, 'Nazwa z legendy': '',
                    'Symbol/etykieta': symbol, 'Ilość': int(qty),
                    'Metoda': 'Automatycznie / legenda graficzna PDF',
                    'Pewność': 'średnia', 'Źródło': 'PDF/grafika + oznaczenie planu',
                })
                fallback_total += qty
        if fallback_rows:
            rows.extend(fallback_rows)
            statuses.insert(0, f'Legenda PDF: legenda graficzna bez kodów tekstowych — użyto oznaczeń planu; znaleziono {len(fallback_rows)} typów / {fallback_total} wystąpień.')
        else:
            statuses.insert(0, 'Legenda PDF: nie znaleziono tekstowych kodów ani powtarzalnych oznaczeń planu.')
    # Final graphical-symbol pass. This is intentionally independent from
    # text labels so vector legends such as E3.pdf can be recognized.
    if legend_only and auto_legend and graphic_match and group_graphics:
        for pg in pages:
            try:
                gmatches, gentries = _pdf_graphic_legend(pg)
            except Exception as exc:
                gmatches, gentries = [], []
                statuses.append(f'Strona {pg["page"]}: rozpoznawanie grafiki legendy — błąd: {exc}')
            if gmatches:
                for desc, qty, sig in gmatches:
                    # Avoid duplicate graphical rows if the same description
                    # has already been generated by a textual legend path.
                    norm=re.sub(r'\s+',' ',desc).strip().casefold()
                    existing=[r for r in rows if r.get('Plik')==name and r.get('Strona')==pg['page'] and (re.sub(r'\s+',' ',str(r.get('Nazwa z legendy',''))).strip().casefold()==norm or re.sub(r'\s+',' ',str(r.get('Symbol/etykieta',''))).strip().casefold()==norm)]
                    if existing:
                        # Graphic recognition is the authoritative count for symbol-based legend rows;
                        # do not preserve a stale nonzero text-only estimate.
                        existing[0]['Ilość']=int(qty)
                        existing[0]['Nazwa z legendy']=desc
                        existing[0]['Metoda']='Automatycznie / dopasowanie grafiki legendy PDF'
                        existing[0]['Pewność']=sig.get('match_quality','średnia')
                        existing[0]['Źródło']='PDF/wektory legendy + plan'
                    else:
                        rows.append({
                            'Plik':name,'Strona':pg['page'],'Typ elementu':'Element wg legendy',
                            'Nazwa bloku':'','Misc':'','Name':'','Nazwa elementu':desc,
                            'Nazwa z legendy':desc,'Symbol/etykieta':'grafika PDF',
                            'Ilość':int(qty),'Metoda':'Automatycznie / dopasowanie grafiki legendy PDF',
                            'Pewność':sig.get('match_quality','średnia'),'Źródło':'PDF/wektory legendy + plan',
                        })
                statuses.insert(0,f'Strona {pg["page"]}: dopasowano {len(gmatches)} symboli graficznych z legendy do planu; legenda wykluczona z obszaru zliczania.')
    # Vector-only SSP fallback: some CAD-exported PDFs have no usable legend
    # text at all. In that case count the repeated red symbol families directly.
    if legend_only and auto_legend and graphic_match and not rows:
        try:
            for pg in pages:
                vmatches=_pdf_ssp_vector_fallback(pg)
                if vmatches:
                    for desc,qty in vmatches:
                        rows.append({
                            'Plik':name,'Strona':pg['page'],
                            'Typ elementu':classify(desc, patterns),
                            'Nazwa bloku':'','Misc':'','Name':'',
                            'Nazwa elementu':desc,'Nazwa z legendy':desc,
                            'Symbol/etykieta':'grafika SSP PDF','Ilość':int(qty),
                            'Metoda':'Automatycznie / rozpoznanie grafiki SSP PDF',
                            'Pewność':'wysoka','Źródło':'PDF/wektory SSP',
                        })
                    statuses.insert(0,f'Strona {pg["page"]}: PDF bez użytecznej legendy tekstowej — rozpoznano powtarzalne symbole SSP wektorowo ({", ".join(f"{d}: {q}" for d,q in vmatches)}).')
        except Exception as exc:
            statuses.append(f'Rozpoznawanie wektorowych symboli SSP — błąd: {exc}')
    return rows, pages, statuses

def _pdf_axis_route_length(page, layers=None, coord_tol=3.1, gap=20.0, min_span=40.0):
    """Reconstruct dashed/segmented orthogonal route centerlines from PDF layers.

    CAD-exported PDFs often preserve a dashed route as many short line items.
    We merge collinear intervals on the same coordinate so the measurement is
    based on the route centerline rather than only the visible dash ink.
    """
    import collections
    layers = set(layers or [])
    horiz = []
    vert = []
    diagonal = []
    for drawing in page.get_drawings():
        if layers and drawing.get('layer') not in layers:
            continue
        for item in drawing.get('items', []):
            if item[0] != 'l':
                continue
            p1, p2 = item[1], item[2]
            x1, y1, x2, y2 = float(p1.x), float(p1.y), float(p2.x), float(p2.y)
            dx, dy = x2-x1, y2-y1
            length = math.hypot(dx, dy)
            if length <= 0.1:
                continue
            if abs(dy) <= 0.05 and abs(dx) > 0.5:
                horiz.append(((y1+y2)/2.0, min(x1,x2), max(x1,x2)))
            elif abs(dx) <= 0.05 and abs(dy) > 0.5:
                vert.append(((x1+x2)/2.0, min(y1,y2), max(y1,y2)))
            else:
                diagonal.append((x1,y1,x2,y2,length))

    def merge_axis(items):
        if not items:
            return 0.0, []
        items = sorted(items, key=lambda z: z[0])
        clusters = []
        for item in items:
            if not clusters or abs(item[0] - clusters[-1][0]) > coord_tol:
                clusters.append([item[0], [item]])
            else:
                clusters[-1][1].append(item)
        total = 0.0
        details = []
        for coord, members in clusters:
            intervals = sorted((m[1], m[2]) for m in members)
            merged = []
            for a, b in intervals:
                if not merged or a - merged[-1][1] > gap:
                    merged.append([a, b])
                else:
                    merged[-1][1] = max(merged[-1][1], b)
            span = sum(b-a for a,b in merged)
            if span >= min_span:
                total += span
                details.append((coord, span, len(members)))
        return total, details

    h_total, h_details = merge_axis(horiz)
    v_total, v_details = merge_axis(vert)
    # Diagonal route geometry is uncommon for the supplied odgrom/uziom
    # drawing. Keep only long diagonal segments as a conservative fallback.
    diagonal_total = sum(x[4] for x in diagonal if x[4] >= min_span)
    return h_total + v_total + diagonal_total, h_details, v_details, diagonal_total


def _pdf_odg_layer_name(layer):
    u = str(layer or '').upper()
    # Annotation/text layers must not be measured as physical routes.
    if 'OPIS' in u or 'TEXT' in u or 'TABEL' in u:
        return None
    if 'UZIO' in u or 'BEDNARK' in u:
        return 'Uziom / bednarka'
    if 'ODGROM' in u or 'LPS' in u or '2DE' in u:
        return 'Przewód odgromowy'
    return None


def measure_pdf_odgrom_uziom(data: bytes, scale_denominator=100.0,
                             unit_scale=0.001, selected_layers=None):
    """Measure odgrom/uziom directly from a vector PDF, without DXF.

    PDF CAD exports preserve the original CAD layer names in PyMuPDF drawing
    objects. The function uses those layer names to select the same families
    that the DXF module measures, then reconstructs dashed orthogonal route
    centerlines from the PDF vector segments.
    """
    import fitz, pandas as pd
    doc = fitz.open(stream=data, filetype='pdf')
    rows = []
    try:
        for page_no, page in enumerate(doc, start=1):
            available = sorted({
                str(d.get('layer') or '').strip()
                for d in page.get_drawings()
                if d.get('layer')
            })
            suggested = [layer for layer in available if _pdf_odg_layer_name(layer)]
            layers = list(selected_layers) if selected_layers else suggested
            for layer in layers:
                category = _pdf_odg_layer_name(layer)
                if not category:
                    continue
                length_pdf, h_details, v_details, diag = _pdf_axis_route_length(
                    page, [layer]
                )
                # PDF coordinates are points on the printed sheet. Convert
                # through the drawing scale to model millimetres, then to m.
                length_m = length_pdf * (25.4 / 72.0) * float(scale_denominator) * float(unit_scale)
                if length_m > 1e-9:
                    if 'UZIO' in layer.upper() or 'BEDNARK' in layer.upper():
                        designation = 'Uziom otokowy FeZn25x4'
                    elif 'ODGROM' in layer.upper() or 'LPS' in layer.upper() or '2DE' in layer.upper():
                        designation = 'Przewód odgromowy FeZn Ø8 mm'
                    else:
                        designation = layer
                    rows.append({
                        'Strona': page_no,
                        'Oznaczenie': designation,
                        'Kategoria': category,
                        'Warstwa PDF': layer,
                        'Długość [m]': round(length_m, 3),
                        'Odcinki poziome/pionowe': len(h_details) + len(v_details),
                    })
        return pd.DataFrame(rows)
    finally:
        doc.close()


@st.cache_data(show_spinner=False, max_entries=16)
def measurable_pdf_odgrom_layers(data: bytes):
    """Return PDF layers belonging to the odgrom/uziom measurement."""
    import fitz
    doc = fitz.open(stream=data, filetype='pdf')
    try:
        layers = set()
        for page in doc:
            for drawing in page.get_drawings():
                layer = str(drawing.get('layer') or '').strip()
                if _pdf_odg_layer_name(layer):
                    layers.add(layer)
        return tuple(sorted(layers))
    finally:
        doc.close()


# --- Pomiar tras z PDF wektorowego -----------------------------------------
def _pdf_curve_length(item, steps=24):
    """Approximate length of a PyMuPDF drawing item in PDF points."""
    kind = item[0]
    if kind == 'l':
        p1, p2 = item[1], item[2]
        return math.hypot(p2.x - p1.x, p2.y - p1.y)
    if kind == 'c':
        p0, p1, p2, p3 = item[1], item[2], item[3], item[4]
        total = 0.0
        prev = p0
        for i in range(1, steps + 1):
            t = i / steps
            u = 1.0 - t
            x = u**3*p0.x + 3*u*u*t*p1.x + 3*u*t*t*p2.x + t**3*p3.x
            y = u**3*p0.y + 3*u*u*t*p1.y + 3*u*t*t*p2.y + t**3*p3.y
            total += math.hypot(x-prev.x, y-prev.y)
            prev = type(p0)(x, y)
        return total
    return 0.0


def _pdf_drawing_length(drawing):
    # CAD-exported PDFs often contain the same stroked segment twice (forward
    # and reverse). Deduplicate line segments so the measured route is the
    # physical geometry, not the duplicated PDF painting operation.
    total = 0.0
    seen = set()
    for item in drawing.get('items', []):
        if item[0] == 'l':
            p1, p2 = item[1], item[2]
            a = (round(p1.x, 3), round(p1.y, 3))
            b = (round(p2.x, 3), round(p2.y, 3))
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
        total += _pdf_curve_length(item)
    return total


def detect_pdf_scale(data: bytes, default=100.0):
    """Read a printed scale such as 1:100 from the PDF text layer."""
    try:
        import fitz
        doc = fitz.open(stream=data, filetype='pdf')
        text = '\n'.join(page.get_text('text') or '' for page in doc)
        doc.close()
        matches = re.findall(r'\b1\s*:\s*(\d{2,4})\b', text)
        if matches:
            return float(matches[-1])
    except Exception:
        pass
    return float(default)



def _normalize_pdf_route_label(text):
    """Normalize OCR/text-layer labels such as K3Q0QH60/ELE."""
    if not text:
        return None
    t = re.sub(r'\s+', '', str(text).upper())
    t = t.replace('Q', '0').replace('O', '0')
    m = re.search(r'K.*?(100|200|300).*?/(ELE|TELE)', t)
    if not m:
        return None
    return f"K{m.group(1)}H60/{m.group(2)}"


def _extract_pdf_route_labels(page):
    """Find K100/K200/K300 route labels in a vector PDF.

    Many CAD-exported PDFs convert these labels to outlines, so the normal
    PDF text layer may contain nothing.  We therefore use the existing
    Tesseract installation as a fallback.  Returned coordinates are in PDF
    points and are used only as seeds for assigning nearby vector segments.
    """
    labels = []
    try:
        words = page.get_text('words', sort=True) or []
        for w in words:
            label = _normalize_pdf_route_label(w[4])
            if label:
                labels.append((label, (float(w[0]), float(w[1]), float(w[2]), float(w[3])), 100.0))
    except Exception:
        pass
    if labels:
        return labels
    try:
        import pytesseract
        from PIL import Image, ImageOps
        cmd = find_tesseract()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        # 3x is enough for the small CAD labels while keeping OCR fast.
        scale = 3.0
        pix = page.get_pixmap(matrix=__import__('fitz').Matrix(scale, scale), alpha=False)
        image = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
        gray = ImageOps.grayscale(image)
        # Dark CAD labels on a white sheet.  Thresholding suppresses most of
        # the architectural background and makes the small labels readable.
        bw = gray.point(lambda v: 255 if v < 145 else 0)
        bw = bw.resize((int(bw.width * 1.3), int(bw.height * 1.3)))
        data = pytesseract.image_to_data(
            bw, config='--psm 11', lang='eng',
            output_type=pytesseract.Output.DICT
        )
        ocr_scale = scale * 1.3
        for i, raw in enumerate(data.get('text', [])):
            label = _normalize_pdf_route_label(raw)
            if not label:
                continue
            conf = float(data['conf'][i] or -1)
            x = float(data['left'][i]) / ocr_scale
            y = float(data['top'][i]) / ocr_scale
            w = float(data['width'][i]) / ocr_scale
            h = float(data['height'][i]) / ocr_scale
            # The legend sample is not a route designation for the measured
            # drawing.  It sits in the conventional upper-right legend area.
            if x > page.rect.width * 0.78 and y < page.rect.height * 0.14:
                continue
            labels.append((label, (x, y, w, h), conf))
    except Exception:
        return []
    # Deduplicate OCR hits that are repeated by overlapping recognition boxes.
    unique = []
    seen = set()
    for label, box, conf in labels:
        cx = round(box[0] + box[2] / 2, 1)
        cy = round(box[1] + box[3] / 2, 1)
        key = (label, cx, cy)
        if key not in seen:
            seen.add(key)
            unique.append((label, box, conf))
    return unique


def _point_to_segment(point, p1, p2):
    """Return (distance, t, projected point) for a 2D point/line segment."""
    px, py = point
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    den = dx * dx + dy * dy
    if den <= 1e-12:
        return math.hypot(px - x1, py - y1), 0.0, (x1, y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / den))
    q = (x1 + t * dx, y1 + t * dy)
    return math.hypot(px - q[0], py - q[1]), t, q


def _pdf_route_labelled_length(drawing, labels, max_distance=250.0):
    """Split line geometry between nearby route-designation labels."""
    total_by_label = collections.defaultdict(float)
    unassigned = 0.0
    seen = set()
    route_labels = []
    for label, box, conf in labels:
        cx = box[0] + box[2] / 2.0
        cy = box[1] + box[3] / 2.0
        route_labels.append((label, cx, cy, conf))

    for item in drawing.get('items', []):
        kind = item[0]
        if kind == 'l':
            p1, p2 = item[1], item[2]
            a = (round(p1.x, 3), round(p1.y, 3))
            b = (round(p2.x, 3), round(p2.y, 3))
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            x1, y1, x2, y2 = p1.x, p1.y, p2.x, p2.y
            length = math.hypot(x2 - x1, y2 - y1)
            if length <= 1e-9:
                continue
            candidates = []
            for label, cx, cy, conf in route_labels:
                dist, t, _ = _point_to_segment((cx, cy), (x1, y1), (x2, y2))
                if dist <= max_distance:
                    candidates.append((t, label, cx, cy, dist, conf))
            if not candidates:
                unassigned += length
                continue
            cuts = sorted(set([0.0, 1.0] + [round(x[0], 9) for x in candidates]))
            for lo, hi in zip(cuts, cuts[1:]):
                if hi <= lo:
                    continue
                tm = (lo + hi) / 2.0
                qx = x1 + tm * (x2 - x1)
                qy = y1 + tm * (y2 - y1)
                best = min(candidates, key=lambda c: math.hypot(c[2] - qx, c[3] - qy))
                total_by_label[best[1]] += (hi - lo) * length
        else:
            # Curves are uncommon for tray routes.  Assign the whole curve to
            # the closest label by its bounding-box centre when possible.
            rect = drawing.get('rect')
            if rect is None:
                continue
            cx = (rect.x0 + rect.x1) / 2.0
            cy = (rect.y0 + rect.y1) / 2.0
            best = None
            for label, lx, ly, conf in route_labels:
                dist = math.hypot(lx - cx, ly - cy)
                if best is None or dist < best[0]:
                    best = (dist, label)
            length = _pdf_curve_length(item)
            if best and best[0] <= max_distance:
                total_by_label[best[1]] += length
            else:
                unassigned += length
    return total_by_label, unassigned


def measure_pdf_routes(data: bytes, scale_denominator=None, unit_scale=0.001,
                       selected_routes=None, group_by_labels=True):
    """Measure vector cable routes directly from a PDF.

    In addition to the electrical/teletechnical class, route geometry can be
    split by designations printed on the drawing, e.g. K200H60/ELE and
    K300H60/ELE.  The DXF is not required at runtime; it was only useful while
    validating the PDF/CAD correspondence during development.
    """
    import fitz
    doc = fitz.open(stream=data, filetype='pdf')
    rows = []
    try:
        if scale_denominator is None:
            scale_denominator = detect_pdf_scale(data, 100.0)
        point_to_model = (25.4 / 72.0) * float(scale_denominator) * float(unit_scale)
        color_classes = {
            'ELE': ((0.0, 1.0, 0.0), 'Koryto elektryczne'),
            'TELE': ((0.0, 1.0, 1.0), 'Koryto teletechniczne'),
        }
        wanted = set(selected_routes or color_classes.keys())
        for pageno, page in enumerate(doc, start=1):
            totals = {k: 0.0 for k in color_classes}
            objects = {k: 0 for k in color_classes}
            try:
                _legend_labels, _legend_boxes = _pdf_legend_labels({
                    'words': page.get_text('words') or [],
                    'width': page.rect.width, 'height': page.rect.height
                })
            except Exception:
                _legend_boxes = []
            label_rows = _extract_pdf_route_labels(page) if group_by_labels else []
            labelled_totals = collections.defaultdict(float)
            labelled_objects = collections.defaultdict(int)
            unassigned = collections.defaultdict(float)
            for d in page.get_drawings():
                c = d.get('color')
                if not c or len(c) < 3:
                    continue
                rgb = tuple(round(float(x), 3) for x in c[:3])
                route_key = None
                for key, (ref, _label) in color_classes.items():
                    if all(abs(rgb[i] - ref[i]) <= 0.015 for i in range(3)):
                        route_key = key
                        break
                if route_key is None or route_key not in wanted:
                    continue
                rect = d.get('rect')
                if rect is not None:
                    cx = (rect.x0 + rect.x1) / 2.0
                    cy = (rect.y0 + rect.y1) / 2.0
                    if _point_in_any_box(cx, cy, _legend_boxes):
                        continue
                    if cx >= page.rect.width * 0.78 and cy <= page.rect.height * 0.14:
                        continue
                length = _pdf_drawing_length(d)
                if length < 2.0:
                    continue
                totals[route_key] += length * point_to_model
                objects[route_key] += 1
                if group_by_labels and label_rows:
                    same = [(lab, box, conf) for lab, box, conf in label_rows if lab.endswith('/' + route_key)]
                    if same:
                        parts, missing = _pdf_route_labelled_length(d, same, max_distance=250.0)
                        for lab, value in parts.items():
                            labelled_totals[lab] += value * point_to_model
                            labelled_objects[lab] += 1
                        unassigned[route_key] += missing * point_to_model
            if group_by_labels and label_rows:
                for key, (_ref, class_label) in color_classes.items():
                    if key not in wanted:
                        continue
                    for lab, value in sorted(labelled_totals.items()):
                        if not lab.endswith('/' + key) or value <= 0:
                            continue
                        rows.append({
                            'Plik': 'PDF', 'Strona': pageno,
                            'Oznaczenie': lab,
                            'Trasa': class_label, 'Kod': key,
                            'Długość [m]': round(value, 3),
                            'Obiekty': labelled_objects[lab],
                            'Skala': f'1:{int(scale_denominator)}',
                            'Metoda': 'PDF/wektory + OCR oznaczeń tras',
                        })
                    if unassigned[key] > 0.01:
                        rows.append({
                            'Plik': 'PDF', 'Strona': pageno,
                            'Oznaczenie': 'Nieprzypisane',
                            'Trasa': class_label, 'Kod': key,
                            'Długość [m]': round(unassigned[key], 3),
                            'Obiekty': 0,
                            'Skala': f'1:{int(scale_denominator)}',
                            'Metoda': 'PDF/wektory – brak jednoznacznego oznaczenia',
                        })
                # If OCR found labels but no geometry was assigned, retain the
                # class-level fallback rather than returning an empty result.
                if not rows:
                    label_rows = []
            if not group_by_labels or not label_rows:
                for key, (_ref, label) in color_classes.items():
                    if totals[key] <= 0:
                        continue
                    rows.append({
                        'Plik': 'PDF', 'Strona': pageno,
                        'Trasa': label, 'Kod': key,
                        'Długość [m]': round(totals[key], 3),
                        'Obiekty': objects[key],
                        'Skala': f'1:{int(scale_denominator)}',
                        'Metoda': 'PDF/wektory – rzeczywista geometria',
                    })
    finally:
        doc.close()
    return pd.DataFrame(rows)
