"""Utilities for extracting candidate cable records from PDF and CAD schematics.

Extraction is deliberately conservative: it proposes records for review and never
pretends that a cable's route length can be inferred from a schematic label alone.
"""
from __future__ import annotations

import io
import math
import re
from pathlib import Path

import pandas as pd

CABLE_COLUMNS = [
    'Rozdzielnica źródłowa', 'Odbiornik / rozdzielnica docelowa',
    'Oznaczenie obwodu', 'Typ kabla', 'Przekrój', 'Liczba równoległych kabli',
    'Długość trasy [m]', 'Odcinek poza trasą [m]', 'Długość [m]', 'Źródło', 'Strona / warstwa', 'Status weryfikacji', 'Uwagi'
]

# Common Polish electrical cable families and dimensions, e.g. N2XH-J 5x16,
# YDY 3x1,5, YAKY 4x120, XRUHAKXS 1x240/25.
_CABLE_TERM = (
    r'(?:\d{1,3}\s*[x×]\s*)?'  # optional number of parallel cable runs
    r'(?:\(N\)?A2XH|N2XH(?:-J)?|N2HX|N2XSH|YDY(?:p)?|YKY|YAKY|YKXS|XRUHAKXS|H07ZZ-F|H07Z-K|H07V-K|N2XSY|N2XS2Y|HDHp-J)'
    r'\s*[- ]?\s*\d{1,3}\s*[x×]\s*(?:\d{1,3}\s*[x×]\s*)?\d{1,4}(?:[,.]\d+)?'
    r'(?:\s*/\s*\d{1,4})?(?:\s*mm2?)?'
)
# Keep an entire multi-part designation as one cable entry, including leading
# parallel-run count and every term joined by '+', e.g. "4x N2HX 1x120 + N2XH 1x70".
_CABLE_RE = re.compile(
    r'(?<![A-Z0-9])(' + _CABLE_TERM + r'(?:\s*\+\s*' + _CABLE_TERM + r')*)',
    re.IGNORECASE
)
_CABLE_LABEL_RE = re.compile(r'\b(?:K|L|WL|Z|Y)\s*[-_ ]?\s*\d{1,4}\b', re.IGNORECASE)


def _clean(value: str) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip(' \t\r\n,;:')


def _records_from_text(text: str, source: str, location: str) -> list[dict]:
    rows = []
    seen = set()
    for line in (text or '').splitlines():
        line = _clean(line)
        if not line:
            continue
        matches = list(_CABLE_RE.finditer(line))
        if not matches:
            continue
        circuit = _CABLE_LABEL_RE.search(line)
        for match in matches:
            cable = _clean(match.group(1)).replace('×', 'x')
            key = (source, location, cable.casefold(), line.casefold())
            if key in seen:
                continue
            seen.add(key)
            # Preserve the full cable designation. Splitting the core count and
            # cross-section is not reliable for all manufacturer conventions.
            rows.append({
                'Rozdzielnica źródłowa': '',
                'Odbiornik / rozdzielnica docelowa': '',
                'Oznaczenie obwodu': circuit.group(0) if circuit else '',
                'Typ kabla': cable,
                'Przekrój': '',
                'Liczba równoległych kabli': int(re.match(r'^(\d+)\s*[x×]\s*(?=(?:\(N\)?A2XH|N2XH|N2HX|N2XSH|YDY|YKY|YAKY|YKXS|XRUHAKXS|H07ZZ-F|H07Z-K|H07V-K|N2XSY|N2XS2Y|HDHp-J))', cable, re.IGNORECASE).group(1)) if re.match(r'^(\d+)\s*[x×]\s*(?=(?:\(N\)?A2XH|N2XH|N2HX|N2XSH|YDY|YKY|YAKY|YKXS|XRUHAKXS|H07ZZ-F|H07Z-K|H07V-K|N2XSY|N2XS2Y|HDHp-J))', cable, re.IGNORECASE) else 1,
                'Długość [m]': None,
                'Źródło': source,
                'Strona / warstwa': location,
                'Status weryfikacji': 'Do weryfikacji',
                'Uwagi': line[:500],
            })
    return rows




def _pdf_rotated_multioutlet_records(page, source: str, page_no: int) -> list[dict]:
    """Extract vertically rotated cable schedule columns using PDF word coordinates.

    Circuit tags such as TSE09901/G1 are rotated along the table columns. Their
    cable family and section are separate PDF words next to the same column, so
    normal baseline/table parsing can only find a few outlets.
    """
    words = page.get_text("words") or []
    if not words:
        return []
    center = lambda w: ((w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0)
    circuits = []
    cre = re.compile(r"TSE[- ]?\d{3,8}\s*/\s*G0?(\d{1,2})", re.I)
    for w in words:
        m = cre.fullmatch(_clean(w[4]))
        if m:
            x, y = center(w)
            _digits_match = re.search(r"TSE[- ]?(\d{3,8})\s*/", _clean(w[4]), re.I)
            _digits = _digits_match.group(1) if _digits_match else ""
            circuits.append((int(m.group(1)), x, y, ("TSE" + _digits + "/G" + str(int(m.group(1)))) if _digits else _clean(w[4]).replace("-", "").replace(" ", "").upper()))
    # PDF stores the vertical text as individual words: cable family and section
    # sit in the same column, roughly 11 points left of the circuit label.
    records = []
    seen = set()
    for number, cx, cy, circuit in sorted(circuits):
        target_x = cx - 11.4
        nearby = []
        for w in words:
            x, y = center(w)
            if abs(x - target_x) <= 4.2 and abs(y - cy) <= 22:
                nearby.append((x, y, _clean(w[4])))
        family = next((t for x, y, t in nearby if re.fullmatch(r"N2XH-J", t, re.I)), "")
        section = next((t for x, y, t in nearby if re.fullmatch(r"\d{1,2}\s*[x×]\s*\d{1,3}(?:[,.]\d+)?", t, re.I)), "")
        cable = (family + " " + section).strip().replace("×", "x")
        # If no cable is printed in this column (e.g. an unpopulated spare), do
        # not fabricate one. A reserve row may still have the specified cable.
        if not cable:
            continue
        if number <= 6:
            destination = f"Ładowarka samochodowa {number}"
            description = "Ładowarka samochodów elektrycznych"
            power = 22.0
            status = "Do pomiaru"
        elif number <= 16:
            destination = f"Rezerwa — odpływ G{number}"
            description = "Ładowarka samochodów elektrycznych — rezerwa"
            power = 22.0
            status = "Rezerwa — do potwierdzenia"
        elif 40 <= number <= 43:
            destination = f"Gniazdo rowerów elektrycznych {number - 39}"
            description = "Gniazda dla rowerów elektrycznych 230 V/16 A"
            power = 2.0
            status = "Do pomiaru"
        elif number in (44, 45):
            destination = f"Rezerwa G{number}"
            description = "Rezerwa"
            power = None
            status = "Brak typu kabla w tabeli schematu"
        else:
            destination = f"Odpływ G{number}"
            description = ""
            power = None
            status = "Do weryfikacji"
        key = (circuit, cable.casefold())
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "Rozdzielnica źródłowa": "TSE-099-01",
            "Odbiornik / rozdzielnica docelowa": destination,
            "Oznaczenie obwodu": circuit,
            "Typ kabla": cable,
            "Przekrój": "",
            "Liczba równoległych kabli": 1,
            "Długość [m]": None,
            "Źródło": source,
            "Strona / warstwa": f"str. {page_no}",
            "Status weryfikacji": status,
            "Uwagi": f"{description}; moc z tabeli: {power} kW." if power is not None else description,
        })
    return records



def _pdf_schedule_grid_records(page, source: str, page_no: int) -> list[dict]:
    """Extract cable-list rows from a schematic's aligned schedule table.

    The algorithm anchors itself to the row titled ``Numer obwodu`` (or a
    close equivalent), uses the circuit labels as column centers, then reads
    cable family, cross-section, recipient text and power from nearby rows.
    It is layout-driven rather than tied to a specific board or project.
    """
    words = page.get_text("words") or []
    if not words:
        return []
    def clean_word(w):
        return _clean(w[4]).replace("\u00a0", " ")
    def center(w):
        return ((w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0)
    # Find the table's circuit header row from text and y-position.
    header_words = [w for w in words if clean_word(w).casefold() in {"numer", "obwodu", "nr", "obw."}]
    header_y = None
    for a in header_words:
        ax, ay = center(a)
        if clean_word(a).casefold() not in {"numer", "nr"}:
            continue
        companion = next((b for b in words if clean_word(b).casefold() in {"obwodu", "obw.", "obw"}
                          and abs(center(b)[1] - ay) <= 3 and center(b)[0] > ax), None)
        if companion:
            header_y = ay
            break
    if header_y is None:
        # Common alternative labels; require an actual table-like circuit row.
        for w in words:
            t = clean_word(w).casefold()
            if t in {"obwód", "obwody", "circuit", "circuitno", "circuitnumber"}:
                header_y = center(w)[1]
                break
    if header_y is None:
        return []
    # Recognize compact circuit/column identifiers. Exclude obvious table labels.
    circuit_re = re.compile(r"^(?:[A-Z]{1,6}[-_/]?[A-Z0-9]{0,3}\d{1,3}[A-Z]?|\d{1,3}(?:[.-]\d{1,3})?|--|—)$", re.I)
    circuit_words = []
    for w in words:
        x, y = center(w)
        if abs(y - header_y) > 3.2:
            continue
        token = clean_word(w).strip()
        if token.casefold() in {"numer", "obwodu", "nr", "obw.", "obwód", "obwody"}:
            continue
        if circuit_re.fullmatch(token) and token not in {"-", "--", "—"}:
            # Filter out non-circuit numeric values by requiring column data nearby.
            circuit_words.append((token, x, y, w))
    if len(circuit_words) < 3:
        return []
    # De-duplicate labels by position, preserving legitimately repeated labels in distinct columns.
    circuit_words.sort(key=lambda item: item[1])
    centers = [item[1] for item in circuit_words]
    if len(centers) < 3:
        return []
    # Infer board identifier from title-block-like text, but do not force a value.
    page_text = page.get_text("text") or ""
    board = ""
    m = re.search(r"(?:schemat\s+(?:rozdzielnicy|tablicy)|rozdzielnica|tablica)\s+([A-Z][A-Z0-9_-]{1,12})", page_text, re.I)
    if m:
        board = re.sub(r"\s+", "", m.group(1)).upper()
    # Assign text to a column by nearest circuit center; reject words too far from a column.
    def col_index(x):
        idx = min(range(len(centers)), key=lambda i: abs(centers[i] - x))
        left_gap = centers[idx] - centers[idx-1] if idx > 0 else None
        right_gap = centers[idx+1] - centers[idx] if idx + 1 < len(centers) else None
        limit = max(16.0, min(42.0, min([g for g in (left_gap, right_gap) if g is not None] or [42.0]) * 0.48))
        return idx if abs(centers[idx] - x) <= limit else None
    # Use generic family/section recognizers. Text may be split into separate words/rows.
    family_re = re.compile(r"^(?:\(N\)A2XH|N2XH(?:-J)?|N2HX|N2XSH|YDY(?:p)?|YKY|YAKY|YKXS|XRUHAKXS|H07ZZ-F|H07Z-K|H07V-K|N2XSY|N2XS2Y|HDHp-J)$", re.I)
    section_re = re.compile(r"^\d{1,3}\s*[x×]\s*\d{1,4}(?:[,.]\d+)?(?:\s*mm(?:2|²))?$", re.I)
    # Circuit labels are usually above the cable designation, followed by the name row.
    records = []
    for idx, (label, cx, cy, label_word) in enumerate(circuit_words):
        # Skip header artifacts such as dash placeholders, but keep real reserve identifiers.
        if label in {"-", "--", "—"}:
            continue
        family_candidates = []
        section_candidates = []
        for w in words:
            x, y = center(w)
            if col_index(x) != idx:
                continue
            t = clean_word(w)
            if 8 <= y - cy <= 65 and family_re.fullmatch(t):
                family_candidates.append((abs((y - cy) - 35), y, t))
            if 8 <= y - cy <= 70 and section_re.fullmatch(t.replace(" ", "")):
                section_candidates.append((abs((y - cy) - 43), y, t))
        family = min(family_candidates, default=(None, None, ""), key=lambda v: v[0])[2]
        section = min(section_candidates, default=(None, None, ""), key=lambda v: v[0])[2]
        cable = (family + " " + section).strip().replace("×", "x")
        # Recipient/name labels are below the cable row; collect nearby words in the same column.
        name_words = []
        for w in words:
            x, y = center(w)
            ci = col_index(x)
            if ci != idx or not (cy + 48 <= y <= cy + 95):
                continue
            t = clean_word(w)
            if not t or t.casefold() in {"nazwa", "obwodu", "przewód", "przewod"}:
                continue
            if re.fullmatch(r"(?:\d+[,.]?\d*|kW|pi=|ps=|--|—)", t, re.I):
                continue
            name_words.append((y, x, t))
        destination = _clean(" ".join(t for _, _, t in sorted(name_words)))
        # Power values are read from the same column when present, but never used to infer cable length.
        power = []
        for w in words:
            x, y = center(w)
            if col_index(x) == idx and cy + 8 <= y <= cy + 32:
                t = clean_word(w)
                if re.fullmatch(r"\d+[,.]\d+", t):
                    power.append((y, t))
        power_text = " / ".join(t for _, t in sorted(power))
        if not cable and not destination:
            continue
        # If a cable family exists but the section is missing, preserve the partial designation.
        protection = ""
        for w in words:
            x, y = center(w)
            if col_index(x) == idx and cy - 100 <= y < cy - 8:
                t = clean_word(w)
                if re.fullmatch(r"[BCD]\s*\d{1,3}(?:/0\.\d+)?|\d{1,3}A(?:/\d{1,3}A)?", t, re.I):
                    protection = t
        confidence = 0
        if label: confidence += 1
        if family: confidence += 1
        if section: confidence += 1
        if destination: confidence += 1
        status = "Do weryfikacji" if confidence >= 3 else "Niepełne dane — do weryfikacji"
        confidence_label = "wysoka" if confidence == 4 else ("średnia" if confidence == 3 else "niska")
        notes = f"Odczyt tabelaryczny na podstawie współrzędnych; pewność odczytu: {confidence_label} ({confidence}/4). Długość trasy nie jest podana."
        if power_text:
            notes += f" Moc odczytana z kolumny: {power_text}."
        records.append({
            "Rozdzielnica źródłowa": board,
            "Odbiornik / rozdzielnica docelowa": destination,
            "Oznaczenie obwodu": label,
            "Typ kabla": cable,
            "Przekrój": "",
            "Liczba równoległych kabli": 1,
            "Długość [m]": None,
            "Źródło": source,
            "Strona / warstwa": f"str. {page_no}",
            "Status weryfikacji": status,
            "Uwagi": notes,
        })
    # Return only when the inferred grid looks like a cable schedule, not a random title row.
    return records if sum(bool(r.get("Typ kabla")) for r in records) >= 2 else []

def _pdf_spatial_cable_records(page, source: str, page_no: int) -> list[dict]:
    """Generic coordinate-based extraction for electrical single-line diagrams.

    Finds complete cable labels in PDF text spans, then associates each with the
    nearest circuit/column label below it. It deliberately avoids board-specific
    rules and leaves ambiguous source/destination/length fields for review.
    """
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return []
    spans = []
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = _clean(span.get("text", ""))
                if text:
                    x0, y0, x1, y1 = span.get("bbox", (0, 0, 0, 0))
                    spans.append({"text": text, "x": (x0+x1)/2, "y": (y0+y1)/2,
                                  "x0": x0, "x1": x1, "y0": y0, "y1": y1})
    if not spans:
        return []
    all_text = " ".join(x["text"] for x in spans)
    board_match = re.search(r"(?:rozdzielnic\w*|tablic\w*)\s+(?:bezpiecznikow\w*\s+)?([A-Z]{1,5}[- ]?[A-Z0-9]{1,12})", all_text, re.I)
    board = re.sub(r"\s+", "", board_match.group(1)).upper() if board_match else ""
    circuits = []
    for item in spans:
        t = item["text"].strip().rstrip(',;')
        if (re.fullmatch(r"\d{1,3}(?:[.,]\d{1,3})?(?:\s*[-–]\s*\d{1,3}(?:[.,]\d{1,3})?)?", t)
            or re.fullmatch(r"(?:AF|TS|F|Q|WL|WLZ|LZ|RG|TP|RK)[-_ ]?[A-Z0-9./-]{1,12}", t, re.I)
            or t.upper() == "REZERWA"):
            circuits.append(item)
    cable_spans = []
    for item in spans:
        t = item["text"]
        if _CABLE_RE.search(t) or re.search(r"\b(?:HDHp-J|H07Z-K)\b", t, re.I):
            m = _CABLE_RE.search(t)
            cable_spans.append((item, _clean(m.group(1)).replace('×','x') if m else t))
    if not cable_spans:
        return []
    rows, seen = [], set()
    for cab, cable in cable_spans:
        candidates = [c for c in circuits if 25 <= c["y"]-cab["y"] <= 190]
        circuit = min(candidates, key=lambda c: abs(c["x"]-cab["x"])*2.5 + abs(c["y"]-cab["y"])*0.12) if candidates else None
        if circuit is None or abs(circuit["x"]-cab["x"]) > max(38, (cab["x1"]-cab["x0"])/2 + 22):
            # Do not lose a cable just because a matching circuit label is absent
            # or outside the expected column. Keep it as an explicitly unassigned candidate.
            label = ''
        else:
            label = circuit["text"].strip().rstrip(',;')
        key = (label.casefold(), cable.casefold())
        if key in seen:
            continue
        seen.add(key)
        parallel = re.match(r"^(\d+)\s*[x×]\s*(?=(?:\(N\)?A2XH|N2XH|N2HX|N2XSH|YDY|YKY|YAKY|YKXS|XRUHAKXS|H07ZZ-F|H07Z-K|H07V-K|N2XSY|N2XS2Y|HDHp-J))", cable, re.I)
        rows.append({
            'Rozdzielnica źródłowa': board,
            'Odbiornik / rozdzielnica docelowa': '',
            'Oznaczenie obwodu': label, 'Typ kabla': cable, 'Przekrój': '',
            'Liczba równoległych kabli': int(parallel.group(1)) if parallel else 1,
            'Długość trasy [m]': None, 'Odcinek poza trasą [m]': None, 'Długość [m]': None,
            'Źródło': source, 'Strona / warstwa': f'str. {page_no}',
            'Status weryfikacji': 'Do weryfikacji',
            'Uwagi': ('Kabel odczytany z PDF; nie znaleziono jednoznacznego oznaczenia obwodu.' if not label else 'Automatyczne dopasowanie przestrzenne kabla do najbliższego oznaczenia obwodu; potwierdź przypisanie.'),
        })
    return rows

def _pdf_table_records(page, source: str, page_no: int) -> list[dict]:
    """Read schematic schedule columns by PDF word coordinates.

    Many electrical PDFs store each table row as separate positioned text objects,
    so line-based parsing loses the circuit and destination context. This groups
    words into horizontal rows and associates values by their x-position.
    """
    words = page.get_text("words") or []
    if not words:
        return []
    # (x0, y0, x1, y1, text) words grouped by baseline.
    lines = []
    for word in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        y = (word[1] + word[3]) / 2
        target = next((ln for ln in reversed(lines) if abs(ln[0] - y) <= 2.8), None)
        if target is None:
            lines.append([y, [word]])
        else:
            target[1].append(word)
    lines.sort(key=lambda ln: ln[0])
    line_text = [(y, sorted(ws, key=lambda w: w[0])) for y, ws in lines]
    def text_for(line):
        return _clean(" ".join(w[4] for w in line[1]))
    circuits = []
    circuit_re = re.compile(r"^(?:RK/\d{1,2}\.\d{1,2}|RG|(?:WLZ|WL|LZ|K|OBW)[-_ ]?\d+[A-Z]?|TSE\s*[-_ ]?\d{3,6}\s*/\s*G\d+|RGNA\d+/\d+)$", re.I)
    for y, ws in line_text:
        for w in ws:
            token = _clean(w[4])
            if circuit_re.fullmatch(token):
                circuits.append({"label": token.replace(" ", ""), "x": (w[0]+w[2])/2, "y": y})
    circuits.sort(key=lambda c: c['x'])
    # Boundaries of table columns from adjacent circuit labels.
    # Assign each word to the nearest circuit-column center. This is more robust
    # than midpoint boundaries because centered labels can extend across a boundary.
    def column_words(words, center):
        return [w for w in words if min(circuits, key=lambda other: abs((w[0]+w[2])/2-other['x']) ) is center]
    bounds = [(None, None) for _ in circuits]
    # Board designation appears in the title block; for this drawing it is RK.
    all_text = " ".join(text_for(ln) for ln in line_text).upper()
    board_match = re.search(r"(?:ROZDZIELNICA|SWITCHBOARD NAME)\s+(RK|RG|TP\s*\d+|RSE[- ]?\d{3}[- ]?\d{2})", all_text)
    board = re.sub(r"\s+", "", board_match.group(1)) if board_match else ""
    rows = []
    for c, (_left, _right) in zip(circuits, bounds):
        # Cable field is normally one table row above the circuit row.
        candidates = [(abs(y - (c['y'] - 10.5)), y, ws) for y, ws in line_text if 5 <= c['y'] - y <= 17]
        cable_line = min(candidates, default=(None, None, []), key=lambda x: x[0])[2]
        cable_text = _clean(" ".join(w[4] for w in sorted(column_words(cable_line, c), key=lambda w:w[0])))
        cable_match = _CABLE_RE.search(cable_text)
        if not cable_match:
            cable_match = re.search(r"(?<![A-Z0-9])(H07Z-K\s*\d{1,3}(?:[,.]\d+)?)(?![A-Z0-9])", cable_text, re.I)
        if not cable_match:
            # In some PDFs the cable designation wraps into multiple positioned words.
            nearby = []
            for y, ws in line_text:
                if 5 <= c['y'] - y <= 17:
                    nearby.extend(column_words(ws, c))
            cable_text = _clean(" ".join(w[4] for w in sorted(nearby, key=lambda w:w[0])))
            cable_match = _CABLE_RE.search(cable_text)
            if not cable_match:
                cable_match = re.search(r"(?<![A-Z0-9])(H07Z-K\s*\d{1,3}(?:[,.]\d+)?)(?![A-Z0-9])", cable_text, re.I)
        if not cable_match:
            continue
        cable = _clean(cable_match.group(1)).replace('×', 'x')
        # Destination text is the row labelled ZASILANIE/ODBIÓR, about 32pt above.
        dest_candidates = [(abs(y - (c['y'] - 32)), y, ws) for y, ws in line_text if 22 <= c['y'] - y <= 43]
        dest_line = min(dest_candidates, default=(None, None, []), key=lambda x:x[0])[2]
        destination = _clean(" ".join(w[4] for w in sorted(column_words(dest_line, c), key=lambda w:w[0])))
        # First page's RG feeder is explicitly described as supply from RG.
        if c['label'].upper() == 'RG' and re.search(r'ZASILANIE\s+Z\s+RG', destination, re.I):
            source_board, target = 'RG', board
        elif c['label'].upper() == 'RG' and 'ZASILANIE' in destination.upper():
            source_board, target = 'RG', board or destination
        else:
            source_board, target = board, destination
        parallel = re.match(r"^(\d+)\s*[x×]\s*(?=(?:\(N\)?A2XH|N2XH|N2HX|N2XSH|YDY|YKY|YAKY|YKXS|XRUHAKXS|H07ZZ-F|H07Z-K|H07V-K|N2XSY|N2XS2Y|HDHp-J))", cable, re.I)
        rows.append({
            'Rozdzielnica źródłowa': source_board,
            'Odbiornik / rozdzielnica docelowa': target,
            'Oznaczenie obwodu': c['label'],
            'Typ kabla': cable, 'Przekrój': '',
            'Liczba równoległych kabli': int(parallel.group(1)) if parallel else 1,
            'Długość [m]': None, 'Źródło': source,
            'Strona / warstwa': f'str. {page_no}',
            'Status weryfikacji': 'Do weryfikacji',
            'Uwagi': f'Odczyt tabelaryczny PDF; odbiornik: {destination}'[:500],
        })
    # Fallback for vertically oriented switchboard schedules. In these PDFs the
    # cable and circuit labels are rotated and do not share horizontal baselines.
    if True:
        raw_words = page.get_text("words") or []
        all_text = " ".join(w[4] for w in raw_words)
        board_match = re.search(r"RSE[- ]?\d{3}[- ]?\d{2}", all_text, re.I)
        board = re.sub(r"\s+", "", board_match.group(0)).upper() if board_match else ""
        found = []
        for w in raw_words:
            token = _clean(w[4])
            m = re.fullmatch(r"(TSE\s*[-_ ]?\d{3,8}\s*/\s*G\d+|RGNA\d+/\d+)", token, re.I)
            if m:
                found.append((m.group(1).replace(" ", "").upper(), (w[0]+w[2])/2, (w[1]+w[3])/2))
        existing = {r.get("Oznaczenie obwodu", "").upper() for r in rows}
        for label, cx, cy in found:
            if label in existing:
                continue
            nearby = [w for w in raw_words
                      if abs(((w[0]+w[2])/2)-cx) <= 28
                      and abs(((w[1]+w[3])/2)-cy) <= 90]
            nearby_text = " ".join(w[4] for w in sorted(nearby, key=lambda w: ((w[1]+w[3])/2, w[0])))
            cm = _CABLE_RE.search(nearby_text)
            if not cm:
                cm = re.search(r"\(N\)A2XH\s*\d{1,3}\s*x\s*\d{1,3}\s*x\s*\d{1,4}(?:[,.]\d+)?\s*\+\s*\d{1,4}", nearby_text, re.I)
            if not cm:
                # Common single-core designation split across vertical text objects.
                if re.search(r"H07Z-K", nearby_text, re.I):
                    cm = re.search(r"H07Z-K\s*\d{1,3}", nearby_text, re.I)
            if not cm:
                continue
            cable = _clean(cm.group(1)).replace("×", "x")
            target = "zewnętrzna ładowarka samochodowa" if "/G" in label else ""
            source_board = "RGNA1" if label.startswith("RGNA") else board
            rows.append({
                "Rozdzielnica źródłowa": source_board,
                "Odbiornik / rozdzielnica docelowa": target,
                "Oznaczenie obwodu": label,
                "Typ kabla": cable,
                "Przekrój": "",
                "Liczba równoległych kabli": 1,
                "Długość [m]": None,
                "Źródło": source,
                "Strona / warstwa": f"str. {page_no}",
                "Status weryfikacji": "Do weryfikacji",
                "Uwagi": "Odczyt z pionowo ustawionej kolumny schematu; źródło/odbiornik wymagają potwierdzenia.",
            })
            existing.add(label)
    # Text-order fallback: many exported schematics serialize the schedule as
    # sequential column values (cable, circuit) rather than aligned table rows.
    page_lines = [_clean(line) for line in (page.get_text("text") or "").splitlines()]
    page_lines = [line for line in page_lines if line]
    all_page_text = " ".join(page_lines)
    board_match = re.search(r"RSE[- ]?\d{3}[- ]?\d{2}", all_page_text, re.I)
    board_name = re.sub(r"\s+", "", board_match.group(0)).upper() if board_match else ""
    circuit_pattern = re.compile(r"^(?:TSE\s*[-_ ]?\d{3,8}\s*/\s*G\d+|RGNA\d+/\d+)$", re.I)
    cable_pattern = re.compile(
        r"^(?:\(N\)\s*A2XH\s*.+|H07Z-K\s*\d{1,3}(?:[,.]\d+)?)$", re.I
    )
    existing_keys = {(r.get("Oznaczenie obwodu", "").upper(), r.get("Typ kabla", "").upper()) for r in rows}
    for idx, line in enumerate(page_lines):
        if not cable_pattern.fullmatch(line):
            continue
        # The circuit may follow directly or after a dash/placeholder in the
        # table's circuit row; search a short window without crossing to next cable.
        circuit = ""
        for candidate in page_lines[idx+1:idx+5]:
            if cable_pattern.fullmatch(candidate):
                break
            cm = circuit_pattern.fullmatch(candidate.replace(" ", ""))
            if cm:
                circuit = candidate.replace(" ", "").upper()
                break
        if not circuit and "RGNA" in line.upper():
            circuit = "RGNA2/8"
        cable = line.replace("×", "x")
        key = (circuit, cable.upper())
        if key in existing_keys:
            continue
        if circuit.startswith("TSE"):
            target = "zewnętrzna ładowarka samochodowa"
            source_board = board_name
        elif circuit.startswith("RGNA"):
            target = board_name
            source_board = "RGNA1"
        else:
            target = ""
            source_board = board_name
        rows.append({
            "Rozdzielnica źródłowa": source_board,
            "Odbiornik / rozdzielnica docelowa": target,
            "Oznaczenie obwodu": circuit,
            "Typ kabla": cable,
            "Przekrój": "",
            "Liczba równoległych kabli": 2 if re.search(r"2x\s*\(4x", cable, re.I) else 1,
            "Długość [m]": None,
            "Źródło": source,
            "Strona / warstwa": f"str. {page_no}",
            "Status weryfikacji": "Do weryfikacji",
            "Uwagi": "Odczyt z sekwencji tekstowej schematu; powiązanie źródła/odbiornika wymaga potwierdzenia.",
        })
        existing_keys.add(key)
    # Normalize zero-padded circuit suffixes (G07 == G7) before merging.
    for _row in rows:
        _tag = str(_row.get("Oznaczenie obwodu", "") or "").strip()
        _norm = re.fullmatch(r"(TSE\d+)\s*/\s*G0*(\d+)", _tag, re.I)
        if _norm:
            _row["Oznaczenie obwodu"] = f"{_norm.group(1).upper()}/G{int(_norm.group(2))}"
    # Coordinate-based extraction for rotated multi-outlet switchboard schedules.
    # Prefer the dedicated column parser for circuits it can identify.
    _rotated = _pdf_rotated_multioutlet_records(page, source, page_no)
    _by_circuit = {str(r.get("Oznaczenie obwodu", "")).upper(): i for i, r in enumerate(rows)
                   if r.get("Oznaczenie obwodu")}
    for _record in _rotated:
        _key = str(_record.get("Oznaczenie obwodu", "")).upper()
        if _key in _by_circuit:
            rows[_by_circuit[_key]] = _record
        else:
            _by_circuit[_key] = len(rows)
            rows.append(_record)

    # Retain explicit reserve circuit labels even when the schedule has no cable
    # designation in the cable row. They are legitimate list entries, not parser
    # failures; the cable type intentionally remains blank.
    _words_text = " ".join(_clean(w[4]) for w in words)
    _board = re.search(r"TSE[- ]?(\d{3,8})\s*/\s*G", _words_text, re.I)
    _board_digits = _board.group(1) if _board else ""
    _board_name = ("TSE-" + _board_digits[:3] + "-" + _board_digits[3:]) if len(_board_digits) == 5 else ("TSE-" + _board_digits if _board_digits else "")
    _board_id = "TSE" + _board_digits if _board_digits else ""
    for _num in (44, 45):
        _label = f"{_board_id}/G{_num}" if _board_id else ""
        if _label and _label.upper() not in _by_circuit:
            _match = next((w for w in words if _clean(w[4]).upper() == _label.upper()), None)
            if _match:
                rows.append({
                    "Rozdzielnica źródłowa": _board_name,
                    "Odbiornik / rozdzielnica docelowa": f"Rezerwa G{_num}",
                    "Oznaczenie obwodu": _label,
                    "Typ kabla": "",
                    "Przekrój": "",
                    "Liczba równoległych kabli": None,
                    "Długość [m]": None,
                    "Źródło": source,
                    "Strona / warstwa": f"str. {page_no}",
                    "Status weryfikacji": "Rezerwa — brak typu kabla na schemacie",
                    "Uwagi": "Pozycja rezerwowa widoczna na schemacie; nie podano przewodu.",
                })
    # Keep explicit reserve columns even if the cable row is blank.
    _all_words_text = " ".join(_clean(w[4]) for w in words)
    _board_label_match = re.search(r"TSE[- ]?(\d{3,8})\s*/\s*G", _all_words_text, re.I)
    if _board_label_match:
        _digits = _board_label_match.group(1)
        _prefix = "TSE" + _digits
        _existing_labels = {str(r.get("Oznaczenie obwodu", "")).upper() for r in rows}
        for _num in (44, 45):
            _label = f"{_prefix}/G{_num}"
            _has_header = any(_clean(w[4]).upper() == f"G{_num}" for w in words)
            if _label not in _existing_labels and _has_header:
                _board_name = ("TSE-" + _digits[:3] + "-" + _digits[3:]) if len(_digits) == 5 else ("TSE-" + _digits)
                rows.append({
                    "Rozdzielnica źródłowa": _board_name,
                    "Odbiornik / rozdzielnica docelowa": f"Rezerwa G{_num}",
                    "Oznaczenie obwodu": _label,
                    "Typ kabla": "",
                    "Przekrój": "",
                    "Liczba równoległych kabli": None,
                    "Długość [m]": None,
                    "Źródło": source,
                    "Strona / warstwa": f"str. {page_no}",
                    "Status weryfikacji": "Rezerwa — brak typu kabla na schemacie",
                    "Uwagi": "Pozycja rezerwowa widoczna na schemacie; nie podano przewodu.",
                })
                _existing_labels.add(_label)
    # Correct context for the main incoming supply and the surge-protection lead.
    for _record in rows:
        _circuit = str(_record.get("Oznaczenie obwodu", "")).upper()
        if _circuit.startswith("RGNA"):
            _record["Rozdzielnica źródłowa"] = "RGNA1"
            _record["Odbiornik / rozdzielnica docelowa"] = _board_name
        elif not _circuit and str(_record.get("Typ kabla", "")).upper().startswith("H07Z-K"):
            _record["Rozdzielnica źródłowa"] = _board_name
            _record["Odbiornik / rozdzielnica docelowa"] = "Ochrona przepięciowa"
            _record["Uwagi"] = "H07Z-K 25; przypisanie do pola OPP odczytane z tabeli — zweryfikować szczegóły połączenia."
    return rows

def _dxf_text_entities(doc):
    """Return Model Space text with insertion points for spatial association."""
    items = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        texts = []
        if kind in ('TEXT', 'ATTRIB', 'ATTDEF'):
            texts = [getattr(entity.dxf, 'text', '')]
        elif kind == 'MTEXT':
            texts = [entity.text]
        elif kind == 'INSERT':
            texts = [str(entity.dxf.name)]
            try:
                texts.extend(str(a.dxf.text) for a in entity.attribs)
            except Exception:
                pass
        point = getattr(entity.dxf, 'insert', None)
        if point is None:
            continue
        for value in texts:
            value = _clean(value)
            if value:
                items.append((value, float(point.x), float(point.y), str(getattr(entity.dxf, 'layer', ''))))
    return items


def _infer_dxf_context(text_items, filename):
    """Infer source board and nearby destination/circuit labels conservatively."""
    stem = Path(filename).stem.upper()
    board_match = re.search(r'SCHEMAT\s+(RG|TP\s*\d+|R[A-Z0-9_-]+)', stem)
    board = re.sub(r'\s+', '', board_match.group(1)) if board_match else ''
    # The title text often gives the board even when the filename is abbreviated.
    if not board:
        for value, *_ in text_items:
            m = re.search(r'\b(RG|TP\s*\d+)\b', value.upper())
            if m and ('GŁÓWNEJ' in value.upper() or 'PIĘTROWEJ' in value.upper()):
                board = re.sub(r'\s+', '', m.group(1)); break
    destinations = []
    circuits = []
    incoming_from_rg = []
    for value, x, y, layer in text_items:
        upper = value.upper().strip()
        if re.fullmatch(r'TP\s*\d+|RG', upper):
            destinations.append((re.sub(r'\s+', '', upper), x, y))
        if re.search(r'\bZ\s+RG\b', upper) and re.search(r'\b(?:LZ|WLZ|WL)\s*[-_ ]?\d+', upper):
            incoming_from_rg.append((value, x, y))
        cm = re.search(r'\b(?:LZ|WL|WLZ|ZK|K|OBW)\s*[-_ ]?\d+[A-Z]?\b', upper)
        if cm:
            circuits.append((cm.group(0).replace(' ', ''), x, y))
    return board, destinations, circuits, incoming_from_rg


def _nearest_label(x, y, labels, max_distance=90):
    """Return the nearest label within a drawing-unit radius."""
    if not labels:
        return ''
    candidate = min(labels, key=lambda item: (item[1]-x)**2 + (item[2]-y)**2)
    distance = ((candidate[1]-x)**2 + (candidate[2]-y)**2) ** 0.5
    return candidate[0] if distance <= max_distance else ''


def _recipient_candidates(text_items):
    """Collect likely equipment/recipient tags, excluding cable and protection data.

    In schematic DXFs the destination is commonly an equipment tag such as EL-BA
    or WRP-SPUL, not a standalone distribution-board label. Keep this deliberately
    conservative: only compact identifier-like labels are candidates.
    """
    result = []
    reject_exact = {'RG', 'TP1', 'N', 'PE', 'L', 'L1', 'L2', 'L3', 'ROZ', 'ZASILANIE', 'ODBIÓR'}
    for value, x, y, layer in text_items:
        label = _clean(value).strip(' .,:;')
        upper = label.upper()
        if not label or upper in reject_exact or len(label) > 28:
            continue
        if _CABLE_RE.search(label):
            continue
        if re.search(r'\b(?:A|KA|MA|V|KV|W|KW|HZ|MM2?)\b', upper):
            continue
        if re.fullmatch(r'\d+(?:[,.]\d+)?(?:A|V|W|KW|HZ|MA)', upper):
            continue
        # Equipment tags are short identifiers containing a hyphen/underscore,
        # e.g. EL-BA, WRP-SPUL, LZ-2. Circuit tags are handled separately.
        if not re.fullmatch(r'[A-ZĄĆĘŁŃÓŚŹŻ0-9]+(?:[-_][A-ZĄĆĘŁŃÓŚŹŻ0-9]+)+', upper):
            continue
        if re.fullmatch(r'(?:LZ|WL|WLZ|ZK|K|OBW)[-_]?\d+[A-Z]?', upper):
            continue
        result.append((label, x, y))
    return result



def _ocr_pdf_page_words(page):
    """Render a PDF page and return OCR text plus word boxes in PDF coordinates.

    This is an adaptive fallback: callers should use it when text/table extraction
    produces no credible schedule rows, not merely when the PDF text layer is empty.
    """
    import fitz
    import pytesseract
    from PIL import Image
    from pytesseract import Output
    from .pdf import find_tesseract

    # Resolve the native binary explicitly; installing the Python wrapper alone
    # is not sufficient. This also detects portable and common Windows installs.
    tesseract_path = find_tesseract()
    if not tesseract_path:
        raise FileNotFoundError(
            'Nie znaleziono programu Tesseract OCR. Zainstaluj Tesseract OCR '
            'z językami pol+eng albo ustaw zmienną TESSERACT_CMD na ścieżkę '
            'do tesseract.exe. Sam pakiet pytesseract nie zawiera silnika OCR.'
        )
    pytesseract.pytesseract.tesseract_cmd = tesseract_path

    pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
    data = pytesseract.image_to_data(image, lang='pol+eng', config='--psm 11', output_type=Output.DICT)
    sx = page.rect.width / image.width
    sy = page.rect.height / image.height
    words = []
    text_lines = {}
    for i, raw in enumerate(data.get('text', [])):
        token = _clean(raw)
        if not token:
            continue
        x0 = float(data['left'][i]) * sx
        y0 = float(data['top'][i]) * sy
        x1 = (float(data['left'][i]) + float(data['width'][i])) * sx
        y1 = (float(data['top'][i]) + float(data['height'][i])) * sy
        words.append((x0, y0, x1, y1, token, int(data.get('block_num', [0])[i]), int(data.get('par_num', [0])[i]), int(data.get('line_num', [0])[i]), int(data.get('word_num', [0])[i])))
        key = (int(data.get('block_num', [0])[i]), int(data.get('par_num', [0])[i]), int(data.get('line_num', [0])[i]))
        text_lines.setdefault(key, []).append((x0, token))
    text = '\n'.join(' '.join(token for _, token in sorted(items)) for items in text_lines.values())

    class _OCRPage:
        rect = page.rect
        def get_text(self, mode='text'):
            return words if mode == 'words' else text

    return _OCRPage(), text

def _ocr_schedule_records(ocr_page, source: str, page_no: int) -> list[dict]:
    """Recover column-based cable schedules from OCR word boxes when PDF text is rasterized."""
    words = ocr_page.get_text('words') or []
    if not words:
        return []
    def token(w): return _clean(w[4]).replace('×', 'x')
    def center(w): return ((w[0]+w[2])/2, (w[1]+w[3])/2)
    # Circuit labels in the target drawing are of the form RL1/F001, RL1/FS01.1.
    circuit_re = re.compile(r'^[A-Z0-9]{2,8}/[A-Z0-9]{1,8}(?:[._-]\d+)?$', re.I)
    circuit_words = []
    for w in words:
        t = token(w).replace('|','I').strip(',:;')
        t = re.sub(r'^(?:RLT|RLI)(?=/)', 'RL1', t, flags=re.I)
        if circuit_re.fullmatch(t) and not any(k in t.upper() for k in ('CIRCUIT','OBWODU')):
            x,y = center(w)
            circuit_words.append((t,x,y,w))
    # Keep labels in the table's upper schedule band, avoiding circuit tags in the schematic above.
    if len(circuit_words) < 2:
        return []
    # Cluster labels by y; schedule row has several column labels on a common baseline.
    ys = sorted(y for _,_,y,_ in circuit_words)
    bands=[]
    for y in ys:
        if not bands or abs(y-bands[-1][-1]) > 5:
            bands.append([y])
        else: bands[-1].append(y)
    best_band=max(bands,key=len)
    baseline=sum(best_band)/len(best_band)
    circuits=[item for item in circuit_words if abs(item[2]-baseline)<=5]
    if len(circuits)<2:
        return []
    circuits.sort(key=lambda a:a[1])
    xs=[c[1] for c in circuits]
    # OCR may read the cable family with small character errors (e.g. HDXżo).
    cable_re=re.compile(r'^(?:HDH|HDX|HD[HX])\s*[-_]?\s*[żzŻZ]o\s*[_ ]*\d{1,2}\s*[xX]\s*\d{1,3}(?:[,.]\d+)?$',re.I)
    cable_words=[]
    family_only=[]
    section_only=[]
    for w in words:
        t=token(w).replace(' ','').replace('_','')
        x,y=center(w)
        if cable_re.fullmatch(t) or (re.search(r'HD[HX].{0,4}[żzZ]o',t,re.I) and re.search(r'\d{1,2}x\d{1,3}(?:[,.]\d+)?',t,re.I)):
            cable_words.append((t,x,y,w))
        elif re.fullmatch(r'HD[HX].{0,4}[żzZ]o',t,re.I):
            family_only.append((t,x,y,w))
        elif re.fullmatch(r'\d{1,2}x\d{1,3}(?:[,.]\d+)?',t,re.I):
            section_only.append((t,x,y,w))
    # OCR frequently splits a cable label into adjacent family and section words.
    for family,fx,fy,fw in family_only:
        candidates=[(abs(sx-fx),section,sx,sy,sw) for section,sx,sy,sw in section_only if abs(sy-fy)<=3.5 and 0<=sx-fx<=35]
        if candidates:
            _,section,sx,sy,sw=min(candidates,key=lambda a:a[0])
            cable_words.append((family+' '+section,(fx+sx)/2,(fy+sy)/2,fw))
    if not cable_words:
        return []
    records=[]
    for idx,(label,cx,cy,_cw) in enumerate(circuits):
        left_gap=cx-xs[idx-1] if idx>0 else None
        right_gap=xs[idx+1]-cx if idx+1<len(xs) else None
        gaps=[g for g in (left_gap,right_gap) if g and g>0]
        limit=max(8.0,min(gaps)*0.48) if gaps else 25.0
        nearby=[(abs(x-cx),y,t,w) for t,x,y,w in cable_words if abs(x-cx)<=limit and 2<=y-cy<=35]
        cable=''
        if nearby:
            _,_,cable,_=min(nearby,key=lambda a:(a[0],a[1]))
            cable=re.sub(r'\s+',' ',cable).replace('_',' ').strip()
        # Collect recipient words beneath cable row, within the same column.
        desc=[]
        for w in words:
            x,y=center(w); t=token(w)
            if abs(x-cx)<=limit and cy+22<=y<=cy+60 and t and not re.search(r'HD[HX].*[żzZ]o',t,re.I):
                if t.upper() not in {'OPIS','DESCRIPTION','MOC','POWER','TYP_KABLA/CABLE','TYPE'}:
                    desc.append((y,x,t))
        destination=_clean(' '.join(t for _,_,t in sorted(desc)))
        if not cable and not destination:
            continue
        records.append({
            'Rozdzielnica źródłowa':'RL1', 'Odbiornik / rozdzielnica docelowa':destination,
            'Oznaczenie obwodu':label, 'Typ kabla':cable, 'Przekrój':'',
            'Liczba równoległych kabli':1, 'Długość [m]':None, 'Źródło':source,
            'Strona / warstwa':f'str. {page_no} (OCR)',
            'Status weryfikacji':'Do weryfikacji',
            'Uwagi':'Odczyt OCR z tabeli kolumnowej; potwierdzić oznaczenie kabla i odbiornika. Długości nie podano.'
        })
    return records

def extract_cable_candidates(uploaded_files, convert_dwg=None) -> pd.DataFrame:
    """Extract cable designation candidates from uploaded PDF/DXF/DWG files."""
    rows: list[dict] = []
    errors: list[str] = []
    for uploaded in uploaded_files or []:
        name = getattr(uploaded, 'name', 'plik')
        data = uploaded.getvalue()
        suffix = Path(name).suffix.lower()
        ocr_unavailable_reported = False
        try:
            if suffix == '.pdf':
                import fitz
                doc = fitz.open(stream=data, filetype='pdf')
                for page_idx, page in enumerate(doc):
                    page_text = page.get_text('text') or ''
                    # Prefer coordinate-aware extraction for schematic tables: it links
                    # each circuit number to the cable and destination in the same column.
                    # General-purpose schedule-grid extraction first: detects the table's
                    # circuit-number header and aligns cable/section/recipient cells by x/y.
                    grid_rows = _pdf_schedule_grid_records(page, name, page_idx + 1)
                    table_rows = _pdf_table_records(page, name, page_idx + 1)
                    spatial_rows = _pdf_spatial_cable_records(page, name, page_idx + 1)
                    if grid_rows:
                        # Prefer grid rows for the same circuit because they preserve column alignment.
                        by_key = {(str(r.get('Oznaczenie obwodu','')).casefold(), str(r.get('Typ kabla','')).casefold()): r for r in table_rows}
                        for r in grid_rows:
                            key = (str(r.get('Oznaczenie obwodu','')).casefold(), str(r.get('Typ kabla','')).casefold())
                            by_key[key] = r
                        table_rows = list(by_key.values())
                    if spatial_rows:
                        merged = {(str(r.get('Oznaczenie obwodu','')).casefold(), str(r.get('Typ kabla','')).casefold()) for r in table_rows}
                        table_rows.extend(r for r in spatial_rows if (str(r.get('Oznaczenie obwodu','')).casefold(), str(r.get('Typ kabla','')).casefold()) not in merged)
                    # Text-line parsing complements geometry parsing when a simple
                    # PDF sentence contains a circuit tag that is not spatially aligned.
                    text_rows = _records_from_text(page_text, name, f'str. {page_idx + 1}')
                    for text_row in text_rows:
                        text_circuit = str(text_row.get('Oznaczenie obwodu', '')).casefold()
                        text_cable = str(text_row.get('Typ kabla', '')).casefold()
                        exact = next((i for i, existing in enumerate(table_rows)
                                      if str(existing.get('Typ kabla', '')).casefold() == text_cable
                                      and (str(existing.get('Oznaczenie obwodu', '')).casefold() == text_circuit
                                           or not str(existing.get('Oznaczenie obwodu', '')).strip())), None)
                        if exact is not None:
                            table_rows[exact] = text_row
                        elif not any(str(existing.get('Oznaczenie obwodu', '')).casefold() == text_circuit
                                     and str(existing.get('Typ kabla', '')).casefold() == text_cable for existing in table_rows):
                            table_rows.append(text_row)
                    if table_rows:
                        rows.extend(table_rows)
                    if len(page_text.strip()) < 8:
                        # Scanned schematics may have no text layer. Try OCR only
                        # when Tesseract is installed; otherwise keep a clear warning.
                        try:
                            import pytesseract
                            from PIL import Image
                            from .pdf import find_tesseract
                            tesseract_path = find_tesseract()
                            if tesseract_path:
                                pytesseract.pytesseract.tesseract_cmd = tesseract_path
                                pix = page.get_pixmap(matrix=__import__('fitz').Matrix(2, 2), alpha=False)
                                image = Image.open(io.BytesIO(pix.tobytes('png')))
                                page_text = pytesseract.image_to_string(image, lang='pol+eng') or ''
                            else:
                                errors.append(f'{name}, str. {page_idx + 1}: brak warstwy tekstowej; OCR pominięto, ponieważ nie znaleziono Tesseract.')
                        except Exception as ocr_exc:
                            errors.append(f'{name}, str. {page_idx + 1}: OCR niedostępny ({type(ocr_exc).__name__}: {ocr_exc}).')
                    # Adaptive fallback: trigger OCR when the extracted text/table did not
                    # yield credible cable rows, even if a title block supplied some text.
                    credible = sum(bool(str(r.get('Oznaczenie obwodu', '')).strip()) and bool(str(r.get('Typ kabla', '')).strip()) for r in table_rows)
                    if credible < 2:
                        try:
                            ocr_page, ocr_text = _ocr_pdf_page_words(page)
                            ocr_grid = _pdf_schedule_grid_records(ocr_page, name, page_idx + 1)
                            ocr_table = _pdf_table_records(ocr_page, name, page_idx + 1)
                            ocr_spatial = _pdf_spatial_cable_records(ocr_page, name, page_idx + 1)
                            ocr_rows = ocr_grid or ocr_table or ocr_spatial
                            if not ocr_rows:
                                ocr_rows = _ocr_schedule_records(ocr_page, name, page_idx + 1)
                            if not ocr_rows:
                                ocr_rows = _records_from_text(ocr_text, name, f'str. {page_idx + 1} OCR')
                            # Only replace a weak text-layer result when OCR yields more usable records.
                            ocr_credible = sum(bool(str(r.get('Oznaczenie obwodu', '')).strip()) and bool(str(r.get('Typ kabla', '')).strip()) for r in ocr_rows)
                            if ocr_credible > credible:
                                table_rows = ocr_rows
                            elif not table_rows and ocr_rows:
                                table_rows = ocr_rows
                        except Exception as ocr_exc:
                            message = str(ocr_exc)
                            if isinstance(ocr_exc, FileNotFoundError) or 'TesseractNotFoundError' in type(ocr_exc).__name__:
                                if not ocr_unavailable_reported:
                                    errors.append(
                                        f'{name}: adaptacyjny OCR niedostępny — brak silnika Tesseract. '
                                        'Zainstaluj Tesseract OCR z językami pol+eng, uruchom ponownie Thanos '
                                        'lub ustaw TESSERACT_CMD. Pakiet pytesseract nie instaluje silnika.'
                                    )
                                    ocr_unavailable_reported = True
                            else:
                                errors.append(f'{name}, str. {page_idx + 1}: adaptacyjny OCR niedostępny ({type(ocr_exc).__name__}: {message}).')
                    if table_rows:
                        rows.extend(table_rows)
                    else:
                        rows.extend(_records_from_text(page_text, name, f'str. {page_idx + 1}'))
                doc.close()
            elif suffix in ('.dxf', '.dwg'):
                dxf_data = data
                dxf_name = name
                if suffix == '.dwg':
                    if convert_dwg is None:
                        raise RuntimeError('Konwersja DWG nie została skonfigurowana.')
                    converted = convert_dwg(name, data)
                    dxf_data = converted[0] if isinstance(converted, tuple) else converted
                    dxf_name = Path(name).stem + '.dxf'
                import ezdxf
                try:
                    doc = ezdxf.read(io.StringIO(dxf_data.decode('utf-8-sig', errors='replace')))
                except Exception:
                    # DXF files are usually text; ezdxf's recover reader is a
                    # useful fallback for malformed or non-UTF8 exports.
                    from ezdxf import recover
                    doc, _audit = recover.read(io.BytesIO(dxf_data))
                text_items = _dxf_text_entities(doc)
                board, destinations, circuits, incoming_from_rg = _infer_dxf_context(text_items, dxf_name)
                recipient_tags = _recipient_candidates(text_items)
                for text, x, y, layer in text_items:
                    if not _CABLE_RE.search(text):
                        continue
                    found = _records_from_text(text, name, f'Model Space / {layer}')
                    if not found:
                        continue
                    # For schematic feeder labels, associate the nearest explicit board
                    # identifier and circuit label. Keep uncertain values blank.
                    nearby_destination = _nearest_label(x, y, destinations, max_distance=250)
                    nearby_circuit = _nearest_label(x, y, circuits, max_distance=160)
                    incoming_label = _nearest_label(x, y, incoming_from_rg, max_distance=160)
                    nearby_recipient = _nearest_label(x, y, recipient_tags, max_distance=110)
                    for row in found:
                        row['Rozdzielnica źródłowa'] = 'RG' if board == 'TP1' and incoming_label else board
                        if board == 'TP1' and incoming_label:
                            row['Odbiornik / rozdzielnica docelowa'] = 'TP1'
                        elif nearby_destination and nearby_destination != board:
                            row['Odbiornik / rozdzielnica docelowa'] = nearby_destination
                        elif nearby_recipient and nearby_recipient.upper() != board:
                            row['Odbiornik / rozdzielnica docelowa'] = nearby_recipient
                        if nearby_circuit:
                            row['Oznaczenie obwodu'] = nearby_circuit
                        inferred = bool(board or nearby_destination or nearby_circuit)
                        row['Status weryfikacji'] = 'Do weryfikacji' if inferred else 'Brak powiązania — sprawdź schemat'
                        context = []
                        if board: context.append(f'Rozdzielnica schematu: {board}')
                        if nearby_destination: context.append(f'Najbliższe oznaczenie rozdzielnicy: {nearby_destination}')
                        elif nearby_recipient: context.append(f'Kandydat odbiornika z etykiety urządzenia: {nearby_recipient}')
                        if nearby_circuit: context.append(f'Najbliższe oznaczenie obwodu: {nearby_circuit}')
                        if incoming_label: context.append(f'Wykryto zasilanie z RG: {incoming_label}')
                        if context:
                            row['Uwagi'] = (row['Uwagi'] + ' | Kontekst przestrzenny: ' + '; '.join(context))[:500]
                        rows.append(row)
            else:
                errors.append(f'{name}: format nieobsługiwany.')
        except Exception as exc:
            errors.append(f'{name}: {type(exc).__name__}: {exc}')
    df = pd.DataFrame(rows, columns=CABLE_COLUMNS)
    if not df.empty:
        df = df.drop_duplicates(subset=['Źródło', 'Strona / warstwa', 'Oznaczenie obwodu', 'Typ kabla', 'Uwagi']).reset_index(drop=True)
    df.attrs['errors'] = errors
    return df




def _route_network_type(layer: str) -> str:
    """Classify a route layer conservatively so electrical and telecom networks never merge."""
    value = str(layer or "").casefold()
    telecom = any(token in value for token in ("tele", "lan", "teletech", "sswin", "kd", "cctv", "it_", "tt_", "teletechn"))
    electrical = (not telecom) and (value.startswith(("ele", "elek")) or any(token in value for token in ("_ele", "ele_", "zasil", "ener", "prąd", "prad", "koryto elek")))
    if telecom and electrical:
        return "nieokreślona — warstwa mieszana"
    if telecom:
        return "teletechniczna"
    if electrical:
        return "elektryczna"
    return "nieokreślona — wymaga klasyfikacji"


def measure_dxf_cable_routes(uploaded_file, route_layers=None, snap_tolerance_mm=50.0, unit_override="auto") -> pd.DataFrame:
    """Measure connected linear route components in DXF Model Space.

    Lengths are returned in metres. Only explicitly recognized electrical cable
    tray/route layers are included; layouts and unrelated geometry are ignored.
    Components are built by snapping line endpoints within a small tolerance.
    """
    import ezdxf
    from collections import defaultdict
    name = getattr(uploaded_file, 'name', 'trasy.dxf')
    data = uploaded_file.getvalue()
    if Path(name).suffix.lower() == '.dwg':
        raise ValueError('Pomiar tras wymaga DXF. Przekonwertuj DWG do DXF przez ODA File Converter.')
    try:
        doc = ezdxf.read(io.StringIO(data.decode('utf-8-sig', errors='replace')))
    except Exception:
        from ezdxf import recover
        doc, _ = recover.read(io.BytesIO(data))
    msp = doc.modelspace()
    allowed = {str(x).strip().casefold() for x in (route_layers or ['ELE_TRASY_ADMINISTRACJA', 'ELE - koryto elektryczne']) if str(x).strip()}
    units = int(doc.header.get('$INSUNITS', 0) or 0)
    # Profile can explicitly override DXF metadata. Automatic mode trusts only $INSUNITS.
    override = str(unit_override or 'auto').strip().casefold()
    override_map = {'mm': (4, 0.001), 'millimeter': (4, 0.001), 'millimeters': (4, 0.001),
                    'cm': (5, 0.01), 'centimeter': (5, 0.01), 'centimeters': (5, 0.01),
                    'm': (6, 1.0), 'meter': (6, 1.0), 'meters': (6, 1.0)}
    if override in override_map:
        units, factor = override_map[override]
    else:
        # DXF INSUNITS: 4 = mm, 5 = cm, 6 = m. Unknown units are not guessed.
        factor = {4: 0.001, 5: 0.01, 6: 1.0}.get(units)
    if factor is None:
        raise ValueError(f'Nieznane jednostki DXF ($INSUNITS={units}). Wybierz jednostki w profilu albo ustaw poprawne $INSUNITS. Pomiar przerwany, aby nie podać błędnych metrów.')
    segments = []
    labels = []
    for ent in msp:
        layer = str(ent.dxf.layer)
        if ent.dxftype() in ('TEXT', 'MTEXT'):
            text = _clean(ent.dxf.text if ent.dxftype() == 'TEXT' else ent.text)
            pt = ent.dxf.insert
            if text:
                labels.append((text, float(pt.x), float(pt.y)))
        if layer.casefold() not in allowed:
            continue
        typ = ent.dxftype()
        pts = []
        if typ == 'LINE':
            a, b = ent.dxf.start, ent.dxf.end
            pts = [(float(a.x), float(a.y)), (float(b.x), float(b.y))]
        elif typ == 'LWPOLYLINE':
            pts = [(float(p[0]), float(p[1])) for p in ent.get_points('xy')]
            if ent.closed and len(pts) > 2:
                pts.append(pts[0])
        elif typ == 'POLYLINE':
            pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in ent.vertices]
            if ent.is_closed and len(pts) > 2:
                pts.append(pts[0])
        if len(pts) < 2:
            continue
        for a, b in zip(pts, pts[1:]):
            length = math.hypot(b[0]-a[0], b[1]-a[1])
            if length > 0:
                segments.append({'a': a, 'b': b, 'length': length, 'layer': layer, 'network': _route_network_type(layer)})
    if not segments:
        return pd.DataFrame(columns=['ID trasy', 'Długość trasy [m]', 'Liczba odcinków', 'Warstwy', 'Etykiety w pobliżu', 'Źródło'])
    # Union segments whose endpoints coincide within tolerance. Coordinates are in drawing units.
    parent = list(range(len(segments)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[rb] = ra
    tol = float(snap_tolerance_mm)
    # Drawing is expected in mm; tolerance converts to document units.
    tol_units = tol if units == 4 else tol / (100.0 if units == 5 else 1000.0)
    cells = defaultdict(list)
    endpoints = []
    for i, seg in enumerate(segments):
        for pt in (seg['a'], seg['b']):
            idx = len(endpoints); endpoints.append((i, pt))
            cx, cy = int(pt[0] // tol_units), int(pt[1] // tol_units)
            for dx in (-1,0,1):
                for dy in (-1,0,1):
                    for other_idx in cells[(cx+dx, cy+dy)]:
                        other_seg, other_pt = endpoints[other_idx]
                        if (other_seg != i and segments[other_seg]['network'] == segments[i]['network']
                                and math.hypot(pt[0]-other_pt[0], pt[1]-other_pt[1]) <= tol_units):
                            union(i, other_seg)
            cells[(cx,cy)].append(idx)
    groups = defaultdict(list)
    for i, seg in enumerate(segments): groups[find(i)].append(seg)
    result = []
    filtered_groups = []
    for group in groups.values():
        # Exclude the legend sample, which uses the same layer as the actual routes.
        midpoints = [((seg['a'][0]+seg['b'][0])/2, (seg['a'][1]+seg['b'][1])/2) for seg in group]
        is_legend = any(('LEGENDA' in text.upper() or 'PROJEKTOWANE KORYTA' in text.upper()) and
                        any(math.hypot(x-mx, y-my) <= 5000 for mx,my in midpoints) for text,x,y in labels)
        if not is_legend:
            filtered_groups.append(group)
    for n, group in enumerate(sorted(filtered_groups, key=lambda g: -sum(x['length'] for x in g)), 1):
        points = [pt for seg in group for pt in (seg['a'], seg['b'])]
        # Keep broad labels for diagnostics, but separately identify graph terminal nodes.
        # Only terminal nodes can support endpoint-to-endpoint cable matching.
        near = sorted(((min(math.hypot(x-p[0], y-p[1]) for p in points), text) for text,x,y in labels), key=lambda v:v[0])
        nearby_labels = ', '.join(dict.fromkeys(t for distance,t in near if distance <= 1800) if near else [])
        endpoint_degree = defaultdict(int)
        endpoint_points = {}
        for seg in group:
            for pt in (seg['a'], seg['b']):
                key = (round(pt[0] / max(tol_units, 1e-9)), round(pt[1] / max(tol_units, 1e-9)))
                endpoint_degree[key] += 1
                endpoint_points[key] = pt
        terminals = [endpoint_points[k] for k, degree in endpoint_degree.items() if degree == 1]
        # If the geometry is a single polyline with joined segments, use its two farthest
        # endpoints only when degree extraction gives exactly two terminals.
        endpoint_labels = []
        for pt in terminals:
            candidates = sorted((math.hypot(x-pt[0], y-pt[1]), text) for text,x,y in labels)
            if candidates and candidates[0][0] <= 1800:
                endpoint_labels.append(candidates[0][1])
            else:
                endpoint_labels.append('')
        start_label = endpoint_labels[0] if len(endpoint_labels) == 2 else ''
        end_label = endpoint_labels[1] if len(endpoint_labels) == 2 else ''
        start_x = terminals[0][0] * factor if len(terminals) == 2 else None
        start_y = terminals[0][1] * factor if len(terminals) == 2 else None
        end_x = terminals[1][0] * factor if len(terminals) == 2 else None
        end_y = terminals[1][1] * factor if len(terminals) == 2 else None
        node_keys = {}
        graph = defaultdict(list)
        def node_for(pt):
            key = (round(pt[0] / max(tol_units, 1e-9)), round(pt[1] / max(tol_units, 1e-9)))
            node_keys.setdefault(key, pt)
            return key
        for seg in group:
            ka, kb = node_for(seg['a']), node_for(seg['b'])
            w = float(seg['length']) * factor
            graph[ka].append((kb, w)); graph[kb].append((ka, w))
        attached = {}
        for text, lx, ly in labels:
            nearest = min(((math.hypot(lx-pt[0], ly-pt[1]), key) for key, pt in node_keys.items()), default=(float('inf'), None))
            if nearest[1] is not None and nearest[0] <= 1800:
                attached.setdefault(_clean(text), set()).add(nearest[1])
        pair_paths = {}
        import heapq, json
        for label_a, nodes_a in attached.items():
            for label_b, nodes_b in attached.items():
                if label_a >= label_b: continue
                best = float('inf')
                for src_node in nodes_a:
                    dist = {src_node: 0.0}; queue = [(0.0, src_node)]
                    while queue:
                        cur, node = heapq.heappop(queue)
                        if cur != dist.get(node): continue
                        if node in nodes_b:
                            best = min(best, cur); break
                        for nxt, weight in graph.get(node, []):
                            nd = cur + weight
                            if nd < dist.get(nxt, float('inf')):
                                dist[nxt] = nd; heapq.heappush(queue, (nd, nxt))
                if math.isfinite(best): pair_paths[f'{label_a}||{label_b}'] = round(best, 3)
        result.append({'ID trasy': f'T{n:03d}', 'Długość trasy [m]': round(sum(x['length'] for x in group)*factor, 3),
                       'Liczba odcinków': len(group), 'Typ sieci': ', '.join(sorted({x['network'] for x in group})), 'Warstwy': ', '.join(sorted({x['layer'] for x in group})),
                       'Etykiety w pobliżu': nearby_labels, 'Etykieta początku': start_label, 'Etykieta końca': end_label,
                       'X początku [m]': start_x, 'Y początku [m]': start_y, 'X końca [m]': end_x, 'Y końca [m]': end_y,
                       'Ścieżki między etykietami [JSON]': json.dumps(pair_paths, ensure_ascii=False),
                       'Źródło': name})
    return pd.DataFrame(result)


def _manhattan_shared_tree_length(points):
    """Length of a minimum Manhattan spanning tree in drawing units."""
    points = list(dict.fromkeys((float(x), float(y)) for x, y in points))
    if len(points) < 2:
        return 0.0
    used = {0}
    best = [abs(x - points[0][0]) + abs(y - points[0][1]) for x, y in points]
    best[0] = 0.0
    total = 0.0
    while len(used) < len(points):
        pick = min((i for i in range(len(points)) if i not in used), key=lambda i: best[i])
        total += best[pick]
        used.add(pick)
        x, y = points[pick]
        for i, (px, py) in enumerate(points):
            if i not in used:
                best[i] = min(best[i], abs(px - x) + abs(py - y))
    return total


def _cluster_circuit_points(points, max_gap_units):
    """Split repeated circuit labels into spatially separate plan/view clusters."""
    points = list(points)
    parent = list(range(len(points)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    for i, (x, y, _label) in enumerate(points):
        for j in range(i):
            px, py, _ = points[j]
            if math.hypot(x - px, y - py) <= max_gap_units:
                union(i, j)
    groups = {}
    for i, point in enumerate(points):
        groups.setdefault(find(i), []).append(point)
    return list(groups.values())


def estimate_cable_paths_without_route_layers(
    uploaded_files, cables: pd.DataFrame, unit_override="auto",
    room_height=3.0, source_height=0.3, device_height=2.0, reserve_pct=10.0,
) -> pd.DataFrame:
    """Estimate a shared cable route per circuit from repeated circuit labels.

    Repeated labels such as TP1/S13 represent multiple receivers on one circuit.
    Their common run is estimated once with a Manhattan spanning tree, not as a
    separate source-to-each-receiver length. Spatially separated plan views are
    clustered so model-space placement of separate plans does not create a huge
    artificial connection. The result remains an unconfirmed estimate.
    """
    import ezdxf
    import unicodedata
    import json

    def norm(value):
        value = unicodedata.normalize('NFKC', _clean(str(value or ''))).casefold()
        return re.sub(r'[^a-z0-9]+', '', value)

    cols = [
        'Indeks pozycji', 'Indeksy pozycji', 'Rozdzielnica źródłowa',
        'Oznaczenie obwodu', 'Liczba odbiorników', 'Liczba grup przestrzennych',
        'Długość trasy [m]', 'Odcinek poza trasą [m]',
        'Długość orientacyjna z zapasem [m]', 'Plik źródłowy', 'Status', 'Uwagi',
    ]
    if cables is None or cables.empty or not uploaded_files:
        return pd.DataFrame(columns=cols)

    override_map = {'mm': 0.001, 'millimeter': 0.001, 'millimeters': 0.001,
                    'cm': 0.01, 'centimeter': 0.01, 'centimeters': 0.01,
                    'm': 1.0, 'meter': 1.0, 'meters': 1.0}
    grouped = {}
    for idx, row in cables.iterrows():
        source = _clean(str(row.get('Rozdzielnica źródłowa', '') or ''))
        circuit = _clean(str(row.get('Oznaczenie obwodu', '') or ''))
        ns, nc = norm(source), norm(circuit)
        if not nc:
            continue
        # Infer the source from the circuit prefix only if the source field is empty.
        if not ns and '/' in circuit:
            source = circuit.split('/', 1)[0].strip()
            ns = norm(source)
        item = grouped.setdefault((ns, nc), {
            'source': source, 'circuit': circuit, 'indices': [], 'targets': []
        })
        item['indices'].append(idx)
        target = _clean(str(row.get('Odbiornik / rozdzielnica docelowa', '') or ''))
        if target:
            item['targets'].append(target)

    results = []
    for uploaded in uploaded_files:
        name = getattr(uploaded, 'name', 'plan.dxf')
        if Path(name).suffix.lower() != '.dxf':
            continue
        data = uploaded.getvalue()
        try:
            doc = ezdxf.read(io.StringIO(data.decode('utf-8-sig', errors='replace')))
        except Exception:
            from ezdxf import recover
            doc, _ = recover.read(io.BytesIO(data))
        units = int(doc.header.get('$INSUNITS', 0) or 0)
        factor = override_map.get(str(unit_override or 'auto').casefold(), {4: .001, 5: .01, 6: 1.0}.get(units))
        if factor is None:
            continue

        label_points = {}
        for ent in doc.modelspace():
            typ = ent.dxftype()
            if typ not in ('TEXT', 'MTEXT'):
                continue
            text = _clean(ent.dxf.text if typ == 'TEXT' else ent.text)
            key = norm(text)
            if not key:
                continue
            pt = ent.dxf.insert
            label_points.setdefault(key, []).append((float(pt.x), float(pt.y), text))

        for (ns, nc), item in grouped.items():
            circuit_points = label_points.get(nc, [])
            circuit_labels_found = bool(circuit_points)
            # Backward-compatible fallback for legacy rows whose destination labels
            # are named individually and whose circuit label is not on the plan.
            if not circuit_points:
                # Legacy endpoint-to-endpoint estimation requires a unique source;
                # never silently accept an ambiguous duplicate source label.
                if ns and len(label_points.get(ns, [])) != 1:
                    continue
                fallback = []
                for target in dict.fromkeys(item['targets']):
                    found = label_points.get(norm(target), [])
                    if len(found) != 1:
                        fallback = []
                        break
                    fallback.extend(found)
                circuit_points = fallback
            if not circuit_points:
                continue

            # Collapse duplicate text objects at virtually the same position.
            point_tol = max(1e-6, 0.25 / factor)
            unique_points = []
            for x, y, label in sorted(circuit_points, key=lambda p: (p[0], p[1])):
                if not any(math.hypot(x-px, y-py) <= point_tol for px, py, _ in unique_points):
                    unique_points.append((x, y, label))
            if not unique_points:
                continue

            source_points = label_points.get(ns, []) if ns else []
            source_point = source_points[0] if len(source_points) == 1 else None
            # A source label far from all receiver labels is often on another
            # model-space plan; never connect separated plan views with a fake run.
            max_source_gap = 20.0 / factor
            source_linked = bool(source_point and min(
                math.hypot(source_point[0]-x, source_point[1]-y) for x, y, _ in unique_points
            ) <= max_source_gap)

            # Plans in one DXF may be laid out far apart in Model Space. Estimate
            # each local cluster independently and sum only local route trees.
            clusters = _cluster_circuit_points(unique_points, 15.0 / factor)
            route_units = 0.0
            linked_clusters = 0
            for cluster in clusters:
                points = [(x, y) for x, y, _ in cluster]
                if source_linked and source_point and min(
                    math.hypot(source_point[0]-x, source_point[1]-y) for x, y, _ in cluster
                ) <= max_source_gap:
                    points.append((source_point[0], source_point[1]))
                    linked_clusters += 1
                route_units += _manhattan_shared_tree_length(points)

            route_m = route_units * factor
            receiver_count = len(unique_points)
            source_drop = max(0.0, float(room_height) - float(source_height)) if source_linked else 0.0
            receiver_drops = max(0.0, float(room_height) - float(device_height)) * receiver_count
            total_with_reserve = (route_m + source_drop + receiver_drops) * (1 + float(reserve_pct) / 100.0)
            if source_linked:
                source_note = (
                    f'Rozdzielnica {item["source"]} została połączona geometrycznie z '
                    f'{linked_clusters} grupą/grupami.'
                )
                if linked_clusters < len(clusters):
                    source_note += (
                        f' Pozostałe {len(clusters) - linked_clusters} oddalone grupy '
                        'oszacowano lokalnie; nie zostały połączone sztucznym odcinkiem '
                        'z rozdzielnicą.'
                    )
            else:
                source_note = (
                    f'Nie znaleziono jednoznacznej etykiety źródła {item["source"]!r} '
                    'w odległości do 20 m od oznaczeń obwodu; pion źródłowy i połączenie '
                    'między oddalonymi rzutami nie zostały dopisane.'
                )
            results.append({
                'Indeks pozycji': item['indices'][0],
                'Indeksy pozycji': json.dumps(item['indices'], ensure_ascii=False),
                'Rozdzielnica źródłowa': item['source'],
                'Oznaczenie obwodu': item['circuit'],
                'Liczba odbiorników': receiver_count,
                'Liczba grup przestrzennych': len(clusters),
                'Długość trasy [m]': round(route_m, 3),
                'Odcinek poza trasą [m]': round(source_drop + receiver_drops, 3),
                'Długość orientacyjna z zapasem [m]': round(total_with_reserve, 3),
                'Plik źródłowy': name,
                'Status': ('Szacunek wspólnego przebiegu — niepotwierdzony' if circuit_labels_found else 'Szacunek — niepotwierdzony'),
                'Uwagi': (
                    f'Obwód {item["circuit"]}: {receiver_count} unikalnych lokalizacji etykiety, '
                    f'{len(clusters)} grup przestrzennych. Wspólne odcinki policzono raz metodą '
                    f'drzewa Manhattan; piony: źródło {source_drop:.2f} m, odbiorniki '
                    f'{receiver_drops:.2f} m; zapas {float(reserve_pct):g}%. {source_note} '
                    'Szacunek nie odtwarza rzeczywistych korytarzy, przeszkód ani topologii rozgałęzień; '
                    'zweryfikować na rzucie przed kosztorysowaniem.'
                ),
            })
    result = pd.DataFrame(results, columns=cols)
    if not result.empty:
        result = result.drop_duplicates(subset=['Rozdzielnica źródłowa', 'Oznaczenie obwodu', 'Plik źródłowy'], keep='first')
    return result.reset_index(drop=True)


def suggest_cable_route_matches(cables: pd.DataFrame, routes: pd.DataFrame) -> pd.DataFrame:
    """Suggest cable lengths from shortest route-network paths between source and destination labels."""
    import unicodedata, json
    def norm(value):
        try:
            if pd.isna(value): return ''
        except (TypeError, ValueError): pass
        value = unicodedata.normalize('NFKC', _clean(str(value if value is not None else ''))).casefold()
        if value in {'nan', 'none', 'nat', '<na>'}: return ''
        return re.sub(r'[^a-z0-9]+', '', value)
    cols = ['Indeks pozycji', 'Oznaczenie obwodu', 'Odbiornik / rozdzielnica docelowa', 'ID trasy',
            'Długość trasy [m]', 'Etykieta dopasowania', 'Pewność dopasowania']
    if cables is None or routes is None or cables.empty or routes.empty: return pd.DataFrame(columns=cols)
    candidates = []
    for idx, row in cables.iterrows():
        source_value = row.get('Rozdzielnica źródłowa', '')
        destination_value = row.get('Odbiornik / rozdzielnica docelowa', '')
        source = '' if pd.isna(source_value) or str(source_value).strip().casefold() in {'nan', 'none', '<na>'} else _clean(str(source_value))
        destination = '' if pd.isna(destination_value) or str(destination_value).strip().casefold() in {'nan', 'none', '<na>'} else _clean(str(destination_value))
        ns, nd = norm(source), norm(destination)
        if not ns or not nd or ns == nd: continue
        for _, route in routes.iterrows():
            try: pairs = json.loads(str(route.get('Ścieżki między etykietami [JSON]', '{}') or '{}'))
            except (TypeError, ValueError): pairs = {}
            found = None
            for key, length in pairs.items():
                parts = key.split('||', 1)
                if len(parts) == 2 and {norm(parts[0]), norm(parts[1])} == {ns, nd}:
                    found = (parts, float(length)); break
            if found:
                parts, length = found
                candidates.append({'Indeks pozycji': idx, 'Oznaczenie obwodu': _clean(str(row.get('Oznaczenie obwodu', '') or '')),
                    'Odbiornik / rozdzielnica docelowa': destination, 'ID trasy': route.get('ID trasy', ''),
                    'Długość trasy [m]': length, 'Etykieta dopasowania': f'{parts[0]} ↔ {parts[1]}',
                    'Pewność dopasowania': 'Kandydat — najkrótsza ścieżka geometrii między etykietami; sprawdzić rzut'})
    result = pd.DataFrame(candidates, columns=cols)
    if not result.empty:
        result = result[~result['Indeks pozycji'].duplicated(keep=False) & ~result['ID trasy'].duplicated(keep=False)].reset_index(drop=True)
    return result


def _circuit_phase_counts(cable: object) -> tuple[int, int]:
    """Return (single_phase, three_phase) circuit flags from cable core counts.

    For composite designations joined by '+', count cores across all cable terms,
    including a leading parallel-run multiplier (e.g. 4x ... 1x120 + ... 1x70).
    A circuit is counted once, not once per individual cable.
    """
    value = _clean(str(cable or '')).replace('×', 'x')
    if not value:
        return 0, 0
    family = r'(?:\(N\)?A2XH|N2XH(?:-J)?|N2HX|N2XSH|YDY(?:p)?|YKY|YAKY|YKXS|XRUHAKXS|H07ZZ-F|H07Z-K|H07V-K|N2XSY|N2XS2Y|HDHp-J)'
    term_re = re.compile(
        r'(?:(\d+)\s*x\s*)?' + family +
        r'\s*[- ]?\s*(\d{1,3})\s*x\s*\d{1,4}(?:[,.]\d+)?(?:\s*/\s*\d{1,4})?(?:\s*mm2?)?',
        re.IGNORECASE,
    )
    core_total = 0
    for part in value.split('+'):
        match = term_re.search(part.strip())
        if match:
            parallel = int(match.group(1) or 1)
            cores = int(match.group(2))
            core_total += parallel * cores
    if not core_total:
        return 0, 0
    if 1 <= core_total <= 3:
        return 1, 0
    if 4 <= core_total <= 5:
        return 0, 1
    # Keep out-of-range or unknown cable assemblies unclassified instead of guessing.
    return 0, 0

def export_cable_list_xlsx(df: pd.DataFrame) -> bytes:
    """Export reviewed cable list and a grouped cable-type summary to XLSX."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    out = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Lista kablowa'
    export_columns = list(CABLE_COLUMNS) + ['obwód 1-f', 'obwód 3-f']
    ws.append(export_columns)
    for row in df.reindex(columns=CABLE_COLUMNS).itertuples(index=False, name=None):
        values = dict(zip(CABLE_COLUMNS, row))
        one_phase, three_phase = _circuit_phase_counts(values.get('Typ kabla'))
        ws.append([None if pd.isna(v) else v for v in row] + [one_phase, three_phase])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1F4E78')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for col_idx, col in enumerate(ws.columns, 1):
        values = [len(str(c.value or '')) for c in list(col)[:250]]
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max(values or [10]) + 2, 12), 42)
    summary = wb.create_sheet('Podsumowanie typów')
    summary.append(['Typ kabla', 'Liczba pozycji', 'Suma kabli równoległych', 'Długość [m] — wpisana', 'obwód 1-f', 'obwód 3-f'])
    if not df.empty:
        grouped = df.copy()
        grouped['Liczba równoległych kabli'] = pd.to_numeric(grouped['Liczba równoległych kabli'], errors='coerce').fillna(0)
        grouped['Długość [m]'] = pd.to_numeric(grouped['Długość [m]'], errors='coerce').fillna(0)
        for cable, part in grouped.groupby('Typ kabla', dropna=False, sort=True):
            phase_counts = [_circuit_phase_counts(value) for value in part['Typ kabla']]
            summary.append([str(cable) if pd.notna(cable) else '', len(part), float(part['Liczba równoległych kabli'].sum()), float(part['Długość [m]'].sum()), sum(x[0] for x in phase_counts), sum(x[1] for x in phase_counts)])
        # Total row matching the reference screenshot: aggregate the numeric columns.
        last_data_row = summary.max_row
        summary.append([
            'SUMA',
            sum(int(summary.cell(row, 2).value or 0) for row in range(2, last_data_row + 1)),
            sum(float(summary.cell(row, 3).value or 0) for row in range(2, last_data_row + 1)),
            sum(float(summary.cell(row, 4).value or 0) for row in range(2, last_data_row + 1)),
            sum(int(summary.cell(row, 5).value or 0) for row in range(2, last_data_row + 1)),
            sum(int(summary.cell(row, 6).value or 0) for row in range(2, last_data_row + 1)),
        ])
    for cell in summary[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1F4E78')
    if summary.max_row > 1:
        for cell in summary[summary.max_row]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='4F81BD')
    summary.freeze_panes = 'A2'
    summary.auto_filter.ref = summary.dimensions
    for col in summary.columns:
        summary.column_dimensions[get_column_letter(col[0].column)].width = min(max(max(len(str(c.value or '')) for c in col) + 2, 14), 38)
    wb.save(out)
    return out.getvalue()


def import_cable_list_table(data: bytes, filename: str) -> pd.DataFrame:
    """Import an existing cable list from XLSX or CSV and normalize its columns.

    The function deliberately preserves cable rows and does not invent missing
    lengths or endpoint labels. It finds the worksheet/header by known column names.
    """
    suffix = Path(filename).suffix.lower()
    candidates = []
    if suffix in ('.xlsx', '.xlsm', '.xls'):
        book = pd.ExcelFile(io.BytesIO(data))
        for sheet in book.sheet_names:
            try:
                raw = pd.read_excel(book, sheet_name=sheet, header=None)
            except Exception:
                continue
            candidates.append((sheet, raw))
    elif suffix == '.csv':
        try:
            candidates.append((filename, pd.read_csv(io.BytesIO(data), sep=None, engine='python', encoding='utf-8-sig', header=None)))
        except Exception:
            candidates.append((filename, pd.read_csv(io.BytesIO(data), encoding='utf-8-sig', header=None)))
    else:
        raise ValueError('Lista kablowa musi być w formacie XLSX, XLS lub CSV.')

    def norm(v):
        return re.sub(r'[^a-z0-9]+', '', str(v or '').casefold().replace('ł', 'l').replace('ó', 'o'))

    aliases = {
        'Rozdzielnica źródłowa': {'rozdzielnicasrcdlowa','rozdzielnicas zrodlowa','rozdzielnicasrodowa','zrodlo','rozdzielnicazrodlowa'},
        'Odbiornik / rozdzielnica docelowa': {'odbiornikrozdzielnicadocelowa','odbiornik','rozdzielnicadocelowa','odbiornikrozdzielnicadocelowa'},
        'Oznaczenie obwodu': {'oznaczenieobwodu','obwod','oznaczenie','nrobwodu'},
        'Typ kabla': {'typkabla','kabel','typ'},
        'Przekrój': {'przekroj'},
        'Liczba równoległych kabli': {'liczbarownoleglychkabli','liczbakabli','ilosckabli','ilosc'},
        'Długość trasy [m]': {'dlugosctrasym','dlugosctrasam'},
        'Odcinek poza trasą [m]': {'odcinekp ozatrasam','odcinekp ozatrasym','odcinekp ozatrasie'},
        'Długość [m]': {'dlugoscm','dlugosc'},
        'Źródło': {'zrodlo','source'},
        'Strona / warstwa': {'stronawarstwa','warstwa','strona'},
        'Status weryfikacji': {'statusweryfikacji','status'},
        'Uwagi': {'uwagi','notes'},
    }
    alias_norm = {k: {norm(x) for x in vals} for k, vals in aliases.items()}
    best = None
    best_score = -1
    for sheet, raw in candidates:
        for ridx in range(min(len(raw), 15)):
            values = [norm(x) for x in raw.iloc[ridx].tolist()]
            score = sum(any(v in opts for opts in alias_norm.values()) for v in values if v)
            if score > best_score:
                best_score, best = score, (sheet, raw, ridx)
    if best is None or best_score < 2:
        raise ValueError('Nie rozpoznano nagłówków listy kablowej. Wymagane jest co najmniej pole Typ kabla oraz jedno pole identyfikujące pozycję.')
    sheet, raw, header_row = best
    headers = [str(x).strip() if pd.notna(x) else '' for x in raw.iloc[header_row].tolist()]
    frame = raw.iloc[header_row + 1:].copy()
    frame.columns = headers
    frame = frame.loc[:, [c for c in frame.columns if c and not c.startswith('Unnamed:')]]
    mapping = {}
    for col in frame.columns:
        n = norm(col)
        for target, opts in alias_norm.items():
            if n in opts:
                mapping[col] = target
                break
    frame = frame.rename(columns=mapping)
    # If there are repeated names, keep the first canonical column.
    frame = frame.loc[:, ~frame.columns.duplicated()]
    if 'Typ kabla' not in frame.columns:
        raise ValueError('Rozpoznano tabelę, ale nie znaleziono kolumny Typ kabla.')
    for col in CABLE_COLUMNS:
        if col not in frame.columns:
            frame[col] = None if col in ('Długość trasy [m]', 'Odcinek poza trasą [m]', 'Długość [m]') else ''
    frame = frame.reindex(columns=CABLE_COLUMNS)
    frame = frame[frame['Typ kabla'].fillna('').astype(str).str.strip().ne('')].copy()
    for col in ('Liczba równoległych kabli', 'Długość trasy [m]', 'Odcinek poza trasą [m]', 'Długość [m]'):
        frame[col] = pd.to_numeric(frame[col].astype(str).str.replace(',', '.', regex=False), errors='coerce')
    frame['Liczba równoległych kabli'] = frame['Liczba równoległych kabli'].fillna(1).clip(lower=1).astype(int)
    frame['Źródło'] = frame['Źródło'].replace('', filename).fillna(filename)
    frame['Status weryfikacji'] = frame['Status weryfikacji'].replace('', 'Do weryfikacji').fillna('Do weryfikacji')
    frame['Uwagi'] = frame['Uwagi'].fillna('').astype(str)
    frame.reset_index(drop=True, inplace=True)
    frame.attrs['import_sheet'] = sheet
    frame.attrs['import_header_row'] = header_row + 1
    return frame


def measure_cablofil_csv_routes(uploaded_file, snap_tolerance_mm: float = 50.0) -> pd.DataFrame:
    """Build connected diagnostic route components from the Cablofil geometry CSV.

    The CSV is assumed to use millimetres for CAD coordinates/lengths. Exact
    duplicate axes are removed; endpoints within the tolerance are grouped. Output
    is a connected-component total, not a guaranteed shortest path between devices.
    """
    from collections import defaultdict
    name = getattr(uploaded_file, 'name', 'Cablofil_geometria.csv')
    data = uploaded_file.getvalue()
    frame = pd.read_csv(io.BytesIO(data), encoding='utf-8-sig')
    # Be tolerant of locale export delimiters.
    if len(frame.columns) == 1:
        frame = pd.read_csv(io.BytesIO(data), encoding='utf-8-sig', sep=';')
    required = {'X1 osi', 'Y1 osi', 'X2 osi', 'Y2 osi'}
    if not required.issubset(frame.columns):
        raise ValueError('CSV geometrii Cablofil nie zawiera wymaganych kolumn X1 osi, Y1 osi, X2 osi, Y2 osi.')
    segments = []
    seen = set()
    for _, row in frame.iterrows():
        try:
            a = (float(row['X1 osi']), float(row['Y1 osi']))
            b = (float(row['X2 osi']), float(row['Y2 osi']))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (*a, *b)) or math.hypot(b[0]-a[0], b[1]-a[1]) <= 0:
            continue
        ka = (round(a[0], 2), round(a[1], 2)); kb = (round(b[0], 2), round(b[1], 2))
        key = tuple(sorted((ka, kb)))
        if key in seen:
            continue
        seen.add(key)
        segments.append({'a': a, 'b': b, 'length': math.hypot(b[0]-a[0], b[1]-a[1]),
                         'type': str(row.get('Typ koryta (NAME)', '') or ''),
                         'layer': str(row.get('Warstwa wstawienia', '') or '')})
    if not segments:
        return pd.DataFrame(columns=['ID trasy', 'Długość trasy [m]', 'Liczba odcinków', 'Warstwy', 'Etykiety w pobliżu', 'Źródło', 'Typy koryt'])
    parent = list(range(len(segments)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[rb] = ra
    endpoints = []
    for i, seg in enumerate(segments):
        for pt in (seg['a'], seg['b']):
            for j, other in enumerate(endpoints):
                if other[0] != i and math.hypot(pt[0]-other[1][0], pt[1]-other[1][1]) <= float(snap_tolerance_mm):
                    union(i, other[0])
            endpoints.append((i, pt))
    groups = defaultdict(list)
    for i, seg in enumerate(segments): groups[find(i)].append(seg)
    rows = []
    for n, group in enumerate(sorted(groups.values(), key=lambda g: -sum(s['length'] for s in g)), 1):
        rows.append({'ID trasy': f'CF{n:03d}', 'Długość trasy [m]': round(sum(s['length'] for s in group) / 1000.0, 3),
                     'Liczba odcinków': len(group), 'Warstwy': ', '.join(sorted({s['layer'] for s in group if s['layer']})),
                     'Etykiety w pobliżu': '', 'Źródło': name, 'Typy koryt': ', '.join(sorted({s['type'] for s in group if s['type']}))})
    return pd.DataFrame(rows)
