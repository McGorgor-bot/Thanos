"""ATH export support for Thanos.

The writer targets the ATH text format observed in a real Norma/Athenasoft
export.  It preserves the supplied ATH header/catalog and rebuilds the
quantity-bearing ELEMENT/POZYCJA part.  Text is encoded as CP1250 and the
[SUMA KONTROLNA] value is the MD5 of the LF-normalised bytes preceding that
section, matching the supplied reference file.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ENCODING = "cp1250"


def _norm_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _blocks(text: str, heading: str) -> list[str]:
    t = _norm_newlines(text)
    return re.findall(rf"\[{heading}\][\n\r]+.*?(?=\n\[|\Z)", t, re.S)


def _field(block: str, key: str, default: str = "") -> str:
    m = re.search(rf"^{re.escape(key)}=(.*)$", block, re.M)
    return m.group(1) if m else default


def _replace_field(block: str, key: str, value: str) -> str:
    pat = rf"^{re.escape(key)}=.*$"
    if re.search(pat, block, re.M):
        return re.sub(pat, f"{key}={value}", block, count=1, flags=re.M)
    return block.rstrip("\n") + f"\n{key}={value}\n"


def parse_ath_template(data: bytes) -> dict[str, Any]:
    text = data.decode(ENCODING, errors="strict")
    t = _norm_newlines(text)
    catalog = []
    for block in _blocks(t, r"RMS ZEST \d+"):
        if _field(block, "ty").startswith("M"):
            name = _field(block, "na").split("\t", 1)[0].strip()
            rid = _field(block, "id").split("\t", 1)[0].strip()
            jm = _field(block, "jm").split("\t", 1)[0].strip()
            ce = _field(block, "ce").split("\t", 1)[0].strip()
            zid = re.search(r"^\[RMS ZEST (\d+)\]", block, re.M)
            catalog.append({"zest_id": zid.group(1) if zid else "", "name": name, "id": rid, "jm": jm, "price": _float(ce), "catalog_price": _float(ce)})

    positions = []
    for block in re.findall(r"\[POZYCJA\]\n.*?(?=\n\[POZYCJA\]|\n\[ELEMENT|\n\[SUMA KONTROLNA\]|\Z)", t, re.S):
        pid = _field(block, "id").strip()
        pdline = _field(block, "pd")
        na = _field(block, "na")
        jm = _field(block, "jm").split("\t", 1)[0].strip()
        kj = _field(block, "kj").split("\t")
        unit_labor = _float(kj[0]) if kj else 0.0
        rms_ids = re.findall(r"\[RMS (\d+)\]", block)
        material_rms = next((x for x in rms_ids if x not in {"410", "413"}), "")
        labor_qty = 0.0
        m410 = re.search(r"\[RMS 410\]\n(.*?)(?=\n\[RMS |\Z)", block, re.S)
        if m410:
            nz = re.search(r"^nz=([^\r\n\t]+)", m410.group(1), re.M)
            if nz:
                labor_qty = _float(nz.group(1))
        knr = ""
        m = re.search(r"KNNR\s+(\d+)\s+(\d+-\d+)", pdline)
        if m:
            knr = f"KNNR {m.group(1)} {m.group(2)}"
        else:
            knr = pdline.strip()
        
        material_price = _float(kj[1]) if len(kj) > 1 else 0.0
        positions.append({"id": pid, "description": na, "unit": jm, "unit_labor": unit_labor, "labor_qty": labor_qty, "labor_rate": unit_labor, "material_rms": material_rms, "material_price": material_price, "knr": knr, "block": block})

    by_rms = {p["material_rms"]: p for p in positions if p["material_rms"]}
    # In many real ATH files the catalog field ce= is 0 while the actual
    # material price is stored at position level in kj=<labor>\t<material>.
    # Prefer the position-level price when available.
    price_by_rms = {}
    for p in positions:
        rid = p.get("material_rms", "")
        price = _float(p.get("material_price", 0))
        if rid and price > 0 and rid not in price_by_rms:
            price_by_rms[rid] = price
    for c in catalog:
        rid = str(c.get("zest_id", ""))
        if price_by_rms.get(rid, 0) > 0:
            c["price"] = price_by_rms[rid]
    return {"text": t, "catalog": catalog, "positions": positions, "by_rms": by_rms, "price_by_rms": price_by_rms}


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except Exception:
        return default


def load_excel(data: bytes, sheet_name: str | None = None) -> pd.DataFrame:
    book = pd.ExcelFile(io.BytesIO(data))
    sheet = sheet_name or ("Podsumowanie" if "Podsumowanie" in book.sheet_names else book.sheet_names[0])
    return pd.read_excel(io.BytesIO(data), sheet_name=sheet).fillna("")


def catalog_dataframe(template: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(template["catalog"], columns=["zest_id", "name", "id", "jm", "price"])


def suggest_mapping(df: pd.DataFrame, template: dict[str, Any], source_column: str = "Nazwa bloku") -> pd.DataFrame:
    cat = template["catalog"]
    by_name = {}
    for c in cat:
        name = c["name"]
        short = re.sub(r"^Oprawa typu\s+", "", name, flags=re.I).strip()
        by_name[name.casefold()] = c
        by_name[short.casefold()] = c
    rows = []
    for _, r in df.iterrows():
        name = str(r.get(source_column, "") or "").strip()
        qty = _float(r.get("Ilość", 0))
        hit = by_name.get(name.casefold())
        # Fallbacks for common Thanos sheets.
        if not hit:
            for col in ("Typ elementu", "Nazwa elementu", "Nazwa z legendy", "Symbol/etykieta"):
                cand = str(r.get(col, "") or "").strip()
                if cand and cand.casefold() in by_name:
                    hit = by_name[cand.casefold()]
                    if not name:
                        name = cand
                    break
        pos = template["by_rms"].get(hit["zest_id"]) if hit else None
        rows.append({
            "Element": name,
            "Ilość": qty,
            "RMS ZEST": hit["zest_id"] if hit else "",
            "RMS ID": hit["id"] if hit else "",
            "Cena materiału": (template.get("price_by_rms", {}).get(hit["zest_id"], hit["price"]) if hit else 0.0),
            "J.m.": (pos["unit"] if pos else (hit["jm"] if hit else "szt.")),
            "KNR": (pos["knr"] if pos else ""),
            "Opis pozycji": (pos["description"] if pos else (hit["name"] if hit else "")),
            "Nazwa materiału": (hit["name"] if hit else ""),
            "Robocizna rg": (pos.get("labor_qty", 0.0) if pos else 0.0),
            "Stawka R-G": (pos.get("labor_rate", pos.get("unit_labor", 0.0)) if pos else 0.0),
            # Kept for backward compatibility with older mapping tables.
            "Robocizna jedn.": (pos.get("labor_rate", pos.get("unit_labor", 0.0)) if pos else 0.0),
            # A catalog hit is a valid material mapping even when the template
            # does not contain an existing [POZYCJA] using that RMS ZEST.
            "Status": ("OK" if hit and pos else ("MATERIAŁ OK / UZUPEŁNIJ KNR" if hit else "BRAK RMS ZEST")),
        })
    return pd.DataFrame(rows)


def _append_element_to_ath_name(base_name: str, element: str) -> str:
    """Append an XLSX element to the *existing* ATH material name.

    The important distinction is that the existing ATH name is the base/prefix
    and is never replaced by the element name.  If the element is already the
    final token of the name, the name is left unchanged.
    """
    base = str(base_name or "").strip()
    elem = str(element or "").strip()
    if not base:
        return elem
    if not elem:
        return base
    # Avoid duplicates such as ``Oprawa typu LN16 LN16``.
    if base.casefold().endswith(elem.casefold()):
        return base
    return f"{base.rstrip()} {elem}"


def apply_material_name_mode(mapping: pd.DataFrame, mode: str = "ATH + nazwa elementu") -> pd.DataFrame:
    """Prepare the editable material-name column before ATH generation.

    ``ATH + nazwa elementu`` deliberately preserves the complete existing
    material name from ATH and adds the XLSX element at the end.
    """
    out = mapping.copy()
    if "Nazwa materiału" not in out.columns:
        out["Nazwa materiału"] = ""
    if mode == "Własna nazwa":
        return out
    vals = []
    for _, r in out.iterrows():
        ath_name = str(r.get("Nazwa materiału", "") or "").strip()
        element = str(r.get("Element", "") or "").strip()
        if mode == "Nazwa elementu z XLSX":
            vals.append(element or ath_name)
        elif mode == "ATH + nazwa elementu":
            vals.append(_append_element_to_ath_name(ath_name, element))
        else:
            vals.append(ath_name)
    out["Nazwa materiału"] = vals
    return out


def _next_zest_ids(catalog: list[dict[str, Any]], count: int) -> list[str]:
    used = {int(str(c.get("zest_id", "0"))) for c in catalog if str(c.get("zest_id", "")).isdigit()}
    n = max(used or {0}) + 1
    result = []
    while len(result) < count:
        if n not in used:
            result.append(str(n))
            used.add(n)
        n += 1
    return result


def _clone_catalog_block(text: str, source_rms: str, new_rms: str, new_name: str) -> str:
    """Clone one [RMS ZEST] catalog block with a new numeric ZEST id/name."""
    pat = rf"(\[RMS ZEST {re.escape(source_rms)}\][\n\r]+.*?)(?=\n\[|\Z)"
    m = re.search(pat, text, re.S)
    if not m:
        raise ValueError(f"Nie znaleziono katalogu RMS ZEST {source_rms} w szablonie ATH.")
    block = m.group(1)
    block = re.sub(rf"^\[RMS ZEST {re.escape(source_rms)}\]$", f"[RMS ZEST {new_rms}]", block, count=1, flags=re.M)
    nm = re.search(r"^na=([^\t\r\n]*)(.*)$", block, re.M)
    if nm:
        suffix = nm.group(2)
        # Preserve any original suffix after the display name (e.g. \t1).
        block = block[:nm.start(1)] + new_name + suffix + block[nm.end(2):]
    insert_at = m.end(1)
    return text[:insert_at] + "\n\n" + block.rstrip("\n") + text[insert_at:]


def _rename_catalog_block(text: str, zest_id: str, new_name: str) -> str:
    pat = rf"(\[RMS ZEST {re.escape(zest_id)}\][\n\r]+.*?)(?=\n\[|\Z)"
    m = re.search(pat, text, re.S)
    if not m:
        return text
    block = m.group(1)
    nm = re.search(r"^na=([^\t\r\n]*)(.*)$", block, re.M)
    if not nm:
        return text
    suffix = nm.group(2)
    block = block[:nm.start(1)] + new_name + suffix + block[nm.end(2):]
    return text[:m.start(1)] + block + text[m.end(1):]


def _prepare_effective_mapping(parsed: dict[str, Any], mapping: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    """Resolve 1→N material names.

    One source RMS ZEST may be used by many XLSX rows. If all rows use the
    same material name, the original RMS is reused. If different names are
    requested for the same source RMS, the first name keeps the original RMS
    and additional names receive cloned RMS ZEST catalog entries.
    """
    rows = []
    for _, row in mapping.iterrows():
        qty = _float(row.get("Ilość", 0))
        if qty <= 0:
            continue
        rows.append(row.copy())

    by_original: dict[str, list[tuple[int, str]]] = {}
    for i, row in enumerate(rows):
        rid = str(row.get("RMS ZEST", "") or "").strip()
        name = str(row.get("Nazwa materiału", "") or "").strip()
        if rid:
            by_original.setdefault(rid, []).append((i, name))

    clones: dict[str, dict[str, Any]] = {}
    new_ids = _next_zest_ids(parsed["catalog"], sum(max(0, len({n.casefold() for _, n in vals if n})) - 1 for vals in by_original.values()))
    new_iter = iter(new_ids)
    for original, vals in by_original.items():
        # Empty names fall back to the source catalog name.
        source_cat = next((c for c in parsed["catalog"] if str(c.get("zest_id")) == original), None)
        source_name = str(source_cat.get("name", "")) if source_cat else ""
        unique_names: list[str] = []
        for _, name in vals:
            name = name or source_name
            if name.casefold() not in [x.casefold() for x in unique_names]:
                unique_names.append(name)
        if not unique_names:
            continue
        name_to_id = {unique_names[0].casefold(): original}
        for name in unique_names[1:]:
            name_to_id[name.casefold()] = next(new_iter)
        for i, name in vals:
            effective = name or source_name
            rows[i]["RMS ZEST"] = name_to_id[effective.casefold()]
        for name in unique_names[1:]:
            new_id = name_to_id[name.casefold()]
            clones[new_id] = {"source_rms": original, "name": name}

    return pd.DataFrame(rows, columns=mapping.columns), clones


def _replace_catalog_quantities(text: str, mapping: pd.DataFrame) -> str:
    """Update catalog quantities, allowing one ATH RMS position to serve many XLSX rows.

    The same ``RMS ZEST`` may intentionally occur on multiple mapping rows.
    In that case the catalog quantity is the SUM of all XLSX quantities, while
    each XLSX row is still exported as its own [POZYCJA].
    """
    t = text
    quantities = {}
    for _, r in mapping.iterrows():
        rid = str(r.get("RMS ZEST", "") or "").strip()
        if rid:
            quantities[rid] = quantities.get(rid, 0.0) + _float(r.get("Ilość", 0))
    for zest_id, qty in quantities.items():
        pat = rf"(\[RMS ZEST {re.escape(zest_id)}\].*?)(?=\n\[|\Z)"
        m = re.search(pat, t, re.S)
        if not m:
            continue
        block = m.group(1)
        if re.search(r"^il=.*$", block, re.M):
            block = re.sub(r"^il=.*$", f"il={_fmt(qty)}", block, count=1, flags=re.M)
        else:
            block += f"\nil={_fmt(qty)}\n"
        t = t[:m.start(1)] + block + t[m.end(1):]
    return t


def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _make_position(pid: int, nu: str, row: pd.Series, template_pos: dict[str, Any], catalog_item: dict[str, Any], use_template_prices: bool = True) -> str:
    qty = _float(row.get("Ilość", 0))
    unit = str(row.get("J.m.", "") or template_pos.get("unit") or catalog_item.get("jm") or "szt.")
    desc = str(row.get("Opis pozycji", "") or template_pos.get("description") or "Element instalacji")
    knr = str(row.get("KNR", "") or template_pos.get("knr") or "")
    if not knr:
        knr = "KNNR 5 0502-03"
    # Template observed format: pd=<publisher>\tKNNR\t5 0502-03\t5\t0502-03\t\t0
    parts = knr.split()
    if len(parts) >= 3 and parts[0].upper() == "KNNR":
        # ATH stores the printed base as two fields: "KNNR" and "5 0502-03".
        # Keeping 5 and 0502-03 in separate tab fields causes Norma to display
        # a duplicated basis.
        knr_full = "\t".join(["KNNR", f"{parts[1]} {parts[2]}"])
        kparts = [parts[1], parts[2]]
    else:
        knr_full = knr
        kparts = ["5", "0502-03"]
    # Preserve the labour quantity (r-g coefficient) and labour rate from the
    # ATH template. These are independent values: e.g. basic lighting may be
    # nz=1.0 with rate 65, while emergency lighting may be nz=1.2 with rate 78.
    # Older mapping tables only have "Robocizna jedn.", so fall back to the
    # template rate in all cases.
    labor_qty = _float(template_pos.get("labor_qty", 0))
    if labor_qty <= 0:
        labor_qty = _float(row.get("Robocizna rg", 0))
    labor = _float(template_pos.get("labor_rate", template_pos.get("unit_labor", 0)))
    labor_total = labor_qty * labor * qty
    # The editor may return 0.0 for an untouched numeric cell after a rerun.
    # A zero is only authoritative when the catalog itself also has zero price.
    catalog_price = _float(catalog_item.get("price", 0))
    entered_price = _float(row.get("Cena materiału", 0))
    template_price = _float(template_pos.get("material_price", 0))
    if use_template_prices and template_price > 0:
        mat_price = template_price
    else:
        mat_price = entered_price if entered_price > 0 or catalog_price <= 0 else catalog_price
    mat_total = mat_price * qty
    aux = mat_total * 0.025
    total = labor_total + mat_total + aux
    material_rms = str(row.get("RMS ZEST", "") or catalog_item.get("zest_id", ""))
    zest_id = material_rms
    # The RMS section id in a position is the ZEST number, not the material database id.
    parts_out = [
        "[POZYCJA]",
        f"id={pid}",
        f"pd=Kancelaria Prezesa Rady Ministrów 2001\t{knr_full}\t{kparts[0]}\t{kparts[1]}\t\t0",
        f"na={desc}",
        "op=0\t0\t0\t0\t0\t0\t0\t0",
        f"nu={nu}",
        # ATH stores the position quantity as a 3-field value in `ob`:
        # calculated quantity, displayed/base quantity, and multiplier flag.
        # A single scalar here can be accepted by our parser but is not
        # interpreted as the position quantity by Norma, which results in a
        # zero/blank quantity after importing the generated ATH.
        f"ob={_fmt(qty)}\t{_fmt(qty)}\t1",
        f"jm={unit}\t000",
        f"kj={_fmt(labor)}\t{_fmt(mat_price)}\t0",
        f"cj={_fmt(labor + mat_price)}",
        "wn=\t\t",
        "",
        "[PRZEDMIAR]",
        f"wo={_fmt(qty)}\t1\t{_fmt(qty)}\t\t\t\t\t\t",
        "",
        "[RMS 410]",
        f"nz={_fmt(labor_qty)}\t0\t{_fmt(labor_qty)}",
        "np=1",
        f"wa={_fmt(labor_total)}",
        f"wb={_fmt(labor_total)}",
        f"il={_fmt(qty)}",
        "",
        f"[RMS {zest_id}]",
        "nz=1\t0\t1",
        "np=1",
        f"wa={_fmt(mat_total)}",
        f"wb={_fmt(mat_total)}",
        f"il={_fmt(qty)}",
        "",
        "[RMS 413]",
        "nz=2.5\t0\t2,5",
        "np=1",
        "op=M",
        f"wa={_fmt(aux)}",
        f"wb={_fmt(aux)}",
        f"il={_fmt(aux)}",
        "",
    ]
    # Only reference auxiliary material 413 when the template actually
    # contains its RMS ZEST catalog entry. Otherwise Norma may display
    # an unresolved "Nieznany" material. The caller passes the real template,
    # so the presence check is done in generate_ath before serialization.
    return "\n".join(parts_out)


def generate_ath(template_bytes: bytes, mapping: pd.DataFrame, project_name: str = "Thanos - kosztorys", append_element_name: bool = False, name_separator: str = " ", use_template_prices: bool = True) -> bytes:
    parsed = parse_ath_template(template_bytes)
    t = parsed["text"]
    mapping = mapping.copy()
    if "Nazwa materiału" not in mapping.columns:
        mapping["Nazwa materiału"] = ""
    if append_element_name:
        mapping = apply_material_name_mode(mapping, "ATH + nazwa elementu")

    valid = []
    invalid = []
    catalog_by_id = {str(c["zest_id"]).strip(): c for c in parsed["catalog"]}

    # Streamlit data_editor may return a stale/altered RMS ZEST cell after a
    # file/template change. Never trust that value blindly: if it is not a
    # valid ZEST id, recover the mapping from the stable element name using
    # the same matching rules as suggest_mapping(). This also protects against
    # accidental paste of an element-name list into the RMS ZEST column.
    def _recover_catalog(element: str):
        e = str(element or "").strip()
        if not e:
            return None
        candidates = []
        for c in parsed["catalog"]:
            name = str(c.get("name", "")).strip()
            short = re.sub(r"^Oprawa typu\s+", "", name, flags=re.I).strip()
            if e.casefold() in {name.casefold(), short.casefold()}:
                candidates.append(c)
        return candidates[0] if len(candidates) == 1 else None

    for _, row in mapping.iterrows():
        qty = _float(row.get("Ilość", 0))
        if qty <= 0:
            continue
        rid = str(row.get("RMS ZEST", "") or "").strip()
        cat = catalog_by_id.get(rid)
        pos = parsed["by_rms"].get(rid) if cat else None

        # Recover from the element name when the UI supplied an invalid RMS id.
        if not cat:
            recovered = _recover_catalog(row.get("Element", ""))
            if recovered:
                rid = str(recovered["zest_id"]).strip()
                row["RMS ZEST"] = rid
                cat = recovered
                pos = parsed["by_rms"].get(rid)

        # RMS ZEST is the material catalogue mapping. A template may contain
        # the catalogue entry without having an existing [POZYCJA] for it.
        # That is still a valid material mapping: build a lightweight fallback
        # position from the editable mapping row and let _make_position use its
        # normal KNR/unit/description defaults. This fixes false mapping errors
        # such as RMS ZEST 4759/4760 existing in the catalogue but not in
        # parsed["by_rms"].
        if cat and not pos:
            pos = {
                "id": "",
                "description": str(row.get("Opis pozycji", "") or cat.get("name", "") or "Element instalacji"),
                "unit": str(row.get("J.m.", "") or cat.get("jm", "") or "szt."),
                "unit_labor": _float(row.get("Stawka R-G", 0)),
                "labor_qty": _float(row.get("Robocizna rg", 0)),
                "labor_rate": _float(row.get("Stawka R-G", 0)),
                "material_rms": rid,
                "material_price": _float(row.get("Cena materiału", 0)) or _float(cat.get("price", 0)),
                "knr": str(row.get("KNR", "") or ""),
                "block": "",
            }

        if not cat:
            invalid.append(f'{row.get("Element", "") or "(bez nazwy)"} [RMS ZEST={rid or "puste"}]')
            continue
        # Keep the synthesized position available to the later 1→N cloning
        # logic as well. This is needed when the ATH catalogue contains an RMS
        # ZEST entry but no original [POZYCJA] references it.
        if rid and rid not in parsed["by_rms"]:
            parsed["by_rms"][rid] = pos
        valid.append((row, pos, cat))

    if invalid:
        raise ValueError(
            "Nieprawidłowe mapowanie RMS ZEST dla: " + ", ".join(invalid[:10])
            + (" ..." if len(invalid) > 10 else "")
        )

    # In the append mode the source of truth for the existing/base name is the
    # original ATH catalog entry, not the editable mapping cell.  This prevents
    # a previous/generated name from replacing the ATH prefix.
    if append_element_name:
        for row, pos, cat in valid:
            element = str(row.get("Element", "") or "").strip()
            row["Nazwa materiału"] = _append_element_to_ath_name(cat.get("name", ""), element)

    # Resolve material names and 1→N RMS ZEST cloning after validation.
    # One source RMS can therefore feed several XLSX rows, and only rows with
    # different requested names receive cloned catalog entries.
    mapping_valid = pd.DataFrame([r for r, _, _ in valid], columns=mapping.columns) if valid else mapping.iloc[0:0].copy()
    # Preserve the resolved validation tuples but allow effective RMS IDs below.
    effective_mapping, clone_plan = _prepare_effective_mapping(parsed, mapping_valid)

    # Clone catalog entries for rows that need a distinct material name.
    for new_id, info in clone_plan.items():
        source = info["source_rms"]
        name = info["name"]
        t = _clone_catalog_block(t, source, new_id, name)
        source_cat = next(c for c in parsed["catalog"] if str(c.get("zest_id")) == source)
        clone_cat = dict(source_cat)
        clone_cat["zest_id"] = new_id
        clone_cat["name"] = name
        parsed["catalog"].append(clone_cat)
        source_pos = parsed["by_rms"].get(source)
        if source_pos:
            clone_pos = dict(source_pos)
            clone_pos["material_rms"] = new_id
            parsed["by_rms"][new_id] = clone_pos
            parsed["price_by_rms"][new_id] = parsed["price_by_rms"].get(source, clone_pos.get("material_price", 0))

    # Apply the effective display name to every RMS actually used. For a split
    # source RMS, the original ZEST keeps the first requested name and each
    # cloned ZEST already carries its own name.
    desired_names: dict[str, str] = {}
    for _, row in effective_mapping.iterrows():
        rid = str(row.get("RMS ZEST", "") or "").strip()
        name = str(row.get("Nazwa materiału", "") or "").strip()
        if rid and name and rid not in desired_names:
            desired_names[rid] = name
    for rid, name in desired_names.items():
        t = _rename_catalog_block(t, rid, name)
        for c in parsed["catalog"]:
            if str(c.get("zest_id")) == rid:
                c["name"] = name

    # Update catalog quantities after effective RMS IDs are assigned. This is
    # important when one source RMS was split into several named materials.
    t = _replace_catalog_quantities(t, effective_mapping)

    # Rebuild validation tuples against effective RMS IDs.
    valid = []
    for _, row in effective_mapping.iterrows():
        rid = str(row.get("RMS ZEST", "") or "").strip()
        cat = next((c for c in parsed["catalog"] if str(c.get("zest_id")) == rid), None)
        pos = parsed["by_rms"].get(rid) if cat else None
        if cat and pos:
            valid.append((row, pos, cat))

    # Keep the real template header and renamed RMS catalog; rebuild the dynamic cost section.
    first_dynamic = re.search(r"^\[ELEMENT 1\]", t, re.M)
    if not first_dynamic:
        raise ValueError("Szablon ATH nie zawiera sekcji [ELEMENT 1].")
    prefix = t[:first_dynamic.start()].rstrip("\n")
    # Update title/date if present without disturbing the rest of the template.
    prefix = re.sub(r"^na=Instalacje elektryczne$", f"na={project_name}", prefix, flags=re.M, count=1)
    prefix = re.sub(r"^dt=.*$", f"dt={datetime.now():%d.%m.%Y}\t1", prefix, flags=re.M, count=1)

    # Group by whether the reference template considers the item basic/emergency.
    basic = [x for x in valid if "0503-02" not in str(x[1].get("knr", ""))]
    emergency = [x for x in valid if "0503-02" in str(x[1].get("knr", ""))]
    sections = []
    pid = 3000
    def element(title: str, num: int, items: list[tuple]) -> str:
        nonlocal pid
        total_l = sum(
            _float(pos.get("labor_qty", 0))
            * _float(pos.get("labor_rate", pos.get("unit_labor", 0)))
            * _float(r.get("Ilość", 0))
            for r, pos, _ in items
        )
        total_m = sum(
            (_float(pos.get("material_price", 0)) if use_template_prices and _float(pos.get("material_price", 0)) > 0 else _float(r.get("Cena materiału", 0)))
            * _float(r.get("Ilość", 0))
            for r, pos, _ in items
        )
        total = total_l + total_m + total_m*0.025
        out=[f"[ELEMENT {num}]", f"na={title}", f"wa={_fmt(total)}", f"kn={_fmt(total_l)}\t{_fmt(total_m + total_m*0.025)}\t0", f"id={2000+num}", f"nu=1.{num}", "wn=\t\t", ""]
        for j,(r,pos,cat) in enumerate(items,1):
            pid += 1
            generated_pos = _make_position(pid, f"{num}.{j}", r, pos, cat, use_template_prices=use_template_prices)
            if not any(str(c.get("zest_id", "")).strip() == "413" for c in parsed["catalog"]):
                generated_pos = re.sub(r"\n\[RMS 413\].*?(?=\n\[|\Z)", "", generated_pos, flags=re.S)
            out.append(generated_pos)
        return "\n".join(out).rstrip("\n")
    if basic: sections.append(element("Oświetlenie podstawowe", 1, basic))
    if emergency: sections.append(element("Oświetlenie awaryjne", 2, emergency))
    if not sections:
        raise ValueError("Brak pozycji możliwych do wygenerowania. Uzupełnij mapowanie KNR/RMS ZEST.")
    content = prefix + "\n\n" + "\n\n".join(sections) + "\n\n"
    checksum = hashlib.md5(content.replace("\r\n", "\n").replace("\r", "\n").encode(ENCODING)).hexdigest()
    body = content + "[SUMA KONTROLNA]\n"
    return (body + checksum).replace("\n", "\r\n").encode(ENCODING)
