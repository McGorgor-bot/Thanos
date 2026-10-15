"""Non-destructive diagnostics for Cablofil route blocks in DXF Model Space.

This module reports longitudinal geometry encoded in block definitions. It does
not claim a bill of quantities: overlapping/duplicate inserts are flagged for
review and all lengths are derived from drawing units.
"""
from __future__ import annotations

import io
import math
import os
import tempfile
from collections import Counter
from typing import Any


def _dist2(a, b):
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def _point(p):
    return (float(p.x), float(p.y))


def analyze_cablofil_blocks(filename: str, raw: bytes) -> dict[str, Any]:
    """Read Cablofil inserts from Model Space and return segment diagnostics.

    Uses the longest line on CP_DETAL in each referenced block as the candidate
    longitudinal axis. If no such line exists, the insert is listed as lacking
    a measurable axis. Exact duplicate geometry and collinear overlaps are
    flagged; no lengths are silently added together or removed.
    """
    try:
        import ezdxf
        from ezdxf.math import Vec3
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(f"Brak biblioteki ezdxf: {exc}") from exc

    suffix = ".dxf"
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        doc = ezdxf.readfile(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    msp = doc.modelspace()
    layer_candidates = [layer.dxf.name for layer in doc.layers
                        if any(x in layer.dxf.name.lower() for x in ("cblf", "cablofil"))]
    inserts = [e for e in msp.query("INSERT")
               if ("cblf" in str(e.dxf.layer).lower() or "cablofil" in str(e.dxf.layer).lower())]
    rows = []
    for idx, ins in enumerate(inserts, 1):
        attrs = {str(a.dxf.tag).upper(): str(a.dxf.text) for a in ins.attribs}
        block = doc.blocks.get(ins.dxf.name)
        candidates = []
        for ent in block:
            if ent.dxftype() != "LINE":
                continue
            if str(ent.dxf.layer).upper() != "CP_DETAL":
                continue
            start = Vec3(ent.dxf.start)
            end = Vec3(ent.dxf.end)
            length = (end - start).magnitude
            if length > 1e-6:
                candidates.append((length, start, end))
        candidates.sort(key=lambda x: x[0], reverse=True)
        axis = candidates[0] if candidates else None
        if axis:
            local_len, local_start, local_end = axis
            matrix = ins.matrix44()
            # Longitudinal block geometry is drawn on offset edges (e.g. y=-25
            # for a 50 mm tray). Use the block's local x-axis as the centreline,
            # not one of those outline edges.
            xs = [float(local_start.x), float(local_end.x)]
            x0, x1 = min(xs), max(xs)
            p1 = matrix.transform(Vec3(x0, 0.0, 0.0))
            p2 = matrix.transform(Vec3(x1, 0.0, 0.0))
            p1xy, p2xy = _point(p1), _point(p2)
            transformed_len = _dist2(p1xy, p2xy)
            status = "kandydat osi centralnej z definicji bloku"
        else:
            local_len = transformed_len = 0.0
            p1xy = p2xy = (None, None)
            status = "brak linii osiowej na CP_DETAL"
        rows.append({
            "ID": idx,
            "Warstwa wstawienia": str(ins.dxf.layer),
            "Typ koryta (NAME)": attrs.get("NAME", ""),
            "Blok": str(ins.dxf.name),
            "X wstawienia": round(float(ins.dxf.insert.x), 3),
            "Y wstawienia": round(float(ins.dxf.insert.y), 3),
            "Obrót [°]": round(float(ins.dxf.rotation), 3),
            "Długość osi bloku [jedn. CAD]": round(float(transformed_len), 3),
            "X1 osi": round(p1xy[0], 3) if p1xy[0] is not None else None,
            "Y1 osi": round(p1xy[1], 3) if p1xy[1] is not None else None,
            "X2 osi": round(p2xy[0], 3) if p2xy[0] is not None else None,
            "Y2 osi": round(p2xy[1], 3) if p2xy[1] is not None else None,
            "Status": status,
        })

    # Exact geometrical duplicate detection independent of endpoint direction.
    endpoint_keys = Counter()
    for row in rows:
        if row["X1 osi"] is None:
            continue
        a = (round(row["X1 osi"], 2), round(row["Y1 osi"], 2))
        b = (round(row["X2 osi"], 2), round(row["Y2 osi"], 2))
        key = tuple(sorted((a, b)))
        endpoint_keys[key] += 1
    for row in rows:
        if row["X1 osi"] is None:
            continue
        a = (round(row["X1 osi"], 2), round(row["Y1 osi"], 2))
        b = (round(row["X2 osi"], 2), round(row["Y2 osi"], 2))
        if endpoint_keys[tuple(sorted((a, b)))] > 1:
            row["Status"] = "duplikat geometryczny — sprawdzić; nie sumować automatycznie"

    # Flag collinear overlaps separately from exact duplicates. This is only a
    # warning: touching/overlapping inserts can be intentional joints or nested
    # block representations, so the tool never merges or sums them automatically.
    for i, left in enumerate(rows):
        if left["X1 osi"] is None or "duplikat geometryczny" in left["Status"]:
            continue
        ax1, ay1, ax2, ay2 = left["X1 osi"], left["Y1 osi"], left["X2 osi"], left["Y2 osi"]
        lh = abs(ay2-ay1) <= 1.0
        for right in rows[i+1:]:
            if right["X1 osi"] is None or "duplikat geometryczny" in right["Status"]:
                continue
            bx1, by1, bx2, by2 = right["X1 osi"], right["Y1 osi"], right["X2 osi"], right["Y2 osi"]
            rh = abs(by2-by1) <= 1.0
            overlap = 0.0
            near_axis = False
            if lh and rh and abs(ay1-by1) <= 1.0:
                overlap = min(max(ax1,ax2),max(bx1,bx2))-max(min(ax1,ax2),min(bx1,bx2))
                near_axis = overlap > 1.0
            elif (not lh) and (not rh) and abs(ax1-bx1) <= 1.0:
                overlap = min(max(ay1,ay2),max(by1,by2))-max(min(ay1,ay2),min(by1,by2))
                near_axis = overlap > 1.0
            if near_axis:
                if "nakłada się współliniowo" not in left["Status"]:
                    left["Status"] = "nakłada się współliniowo z innym blokiem — sprawdzić"
                if "nakłada się współliniowo" not in right["Status"]:
                    right["Status"] = "nakłada się współliniowo z innym blokiem — sprawdzić"

    # Conservative summary: exact geometrical duplicates are counted once in the
    # diagnostic net total, but near/collinear overlaps remain warnings and are
    # NOT automatically subtracted because they can represent separate trays.
    valid = [r for r in rows if r["X1 osi"] is not None]
    exact_seen = {}
    unique_rows = []
    for row in valid:
        a = (round(row["X1 osi"], 2), round(row["Y1 osi"], 2))
        b = (round(row["X2 osi"], 2), round(row["Y2 osi"], 2))
        key = tuple(sorted((a, b)))
        if key not in exact_seen:
            exact_seen[key] = row
            unique_rows.append(row)
    def _length_total(items):
        return round(sum(float(r.get("Długość osi bloku [jedn. CAD]") or 0) for r in items), 3)
    raw_by_type = Counter()
    unique_by_type = Counter()
    raw_length_by_type = Counter()
    unique_length_by_type = Counter()
    for row in valid:
        typ = row.get("Typ koryta (NAME)") or "(brak NAME)"
        raw_by_type[typ] += 1
        raw_length_by_type[typ] += float(row.get("Długość osi bloku [jedn. CAD]") or 0)
    for row in unique_rows:
        typ = row.get("Typ koryta (NAME)") or "(brak NAME)"
        unique_by_type[typ] += 1
        unique_length_by_type[typ] += float(row.get("Długość osi bloku [jedn. CAD]") or 0)
    diagnostic_summary = [{
        "Typ koryta": typ,
        "Liczba osi surowych": raw_by_type[typ],
        "Liczba osi po usunięciu identycznych duplikatów": unique_by_type[typ],
        "Suma surowa [jedn. CAD]": round(raw_length_by_type[typ], 3),
        "Suma po usunięciu identycznych duplikatów [jedn. CAD]": round(unique_length_by_type[typ], 3),
        "Ostrzeżenie": "Nakładanie częściowe nie jest odejmowane automatycznie"
    } for typ in sorted(raw_by_type)]
    counts = Counter(r["Typ koryta (NAME)"] or "(brak NAME)" for r in rows)
    units = int(doc.header.get("$INSUNITS", 0) or 0)
    unit_names = {0: "nieokreślone", 1: "cale", 2: "stopy", 4: "mm", 5: "cm", 6: "m"}
    return {
        "plik": filename,
        "liczba_wstawień": len(inserts),
        "liczba_osi_z_geometrii": len(valid),
        "liczba_bez_osi": len(inserts) - len(valid),
        "jednostki_kodu": units,
        "jednostki_opis": unit_names.get(units, f"kod {units}"),
        "warstwy_kandydackie": layer_candidates,
        "typy": [{"Typ koryta": k, "Liczba bloków": v} for k, v in sorted(counts.items())],
        "elementy": rows,
        "podsumowanie_diagnostyczne": diagnostic_summary,
        "liczba_osi_unikalnych_geometrycznie": len(unique_rows),
        "suma_długości_surowa_jedn_CAD": _length_total(valid),
        "suma_długości_po_usunięciu_ident_dubl_jedn_CAD": _length_total(unique_rows),
        "uwaga": "To diagnostyka osi z definicji bloków, nie zatwierdzony przedmiar. Długości są w jednostkach CAD. Nakładanie się odcinków, bloki zagnieżdżone i elementy specjalne wymagają kontroli; nie sumuj bez sprawdzenia.",
    }
