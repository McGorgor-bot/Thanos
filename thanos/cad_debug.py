import io, math, os, re, shutil, subprocess, tempfile, time
from pathlib import Path
import pandas as pd
import streamlit as st
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from .config import RESULT_COLUMNS
from .state import _file_content_cache_key
from .classification import classify

class _MemoryFile:
    """Minimal UploadedFile-compatible wrapper for generated DXF bytes."""
    def __init__(self, name, data, display_name=None, source_name=None):
        self.name = str(name)
        self._data = bytes(data)
        self.size = len(self._data)
        self.display_name = display_name or self.name
        self.source_name = source_name or self.name

    def getvalue(self):
        return self._data


def find_oda_converter():
    """Locate an installed ODA File Converter automatically on Windows."""
    candidates = [
        shutil.which('ODAFileConverter.exe'),
        shutil.which('ODAFileConverter'),
        r'C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe',
        r'C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe',
        r'C:\Program Files (x86)\ODA\ODAFileConverter\ODAFileConverter.exe',
        r'C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe',
        os.path.expandvars(r'%LOCALAPPDATA%\Programs\ODA\ODAFileConverter\ODAFileConverter.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\ODA\ODAFileConverter\ODAFileConverter.exe'),
    ]
    for raw in candidates:
        if not raw:
            continue
        text = str(raw)
        if '*' in text:
            try:
                parent = Path(text).parent
                pattern = Path(text).name
                for p in sorted(parent.glob(pattern)):
                    if p.is_file():
                        return str(p)
            except Exception:
                continue
        elif Path(text).is_file():
            return str(Path(text))

    # Last automatic attempt: inspect common ODA installation roots without
    # scanning the whole disk.
    for root_text in (
        os.environ.get('ProgramFiles'),
        os.environ.get('ProgramFiles(x86)'),
        os.environ.get('LOCALAPPDATA'),
    ):
        if not root_text:
            continue
        root = Path(root_text)
        if not root.exists():
            continue
        try:
            matches = list(root.glob('ODA/**/ODAFileConverter.exe'))
            if matches:
                return str(sorted(matches, key=lambda p: str(p).lower())[0])
        except Exception:
            pass
    return None


def _hide_windows_for_process(pid):
    """Hide top-level windows owned by the ODA process on Windows.

    ODA File Converter is a GUI-subsystem application: CREATE_NO_WINDOW alone
    does not suppress its Qt window.  We therefore hide any windows belonging
    to the converter PID while its command-line conversion is running.
    """
    if os.name != 'nt' or not pid:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        enum_windows = user32.EnumWindows
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        enum_windows.argtypes = [callback_type, wintypes.LPARAM]
        enum_windows.restype = wintypes.BOOL
        get_pid = user32.GetWindowThreadProcessId
        get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        get_pid.restype = wintypes.DWORD
        show_window = user32.ShowWindow
        show_window.argtypes = [wintypes.HWND, ctypes.c_int]
        show_window.restype = wintypes.BOOL

        @callback_type
        def _enum(hwnd, _lparam):
            owner_pid = wintypes.DWORD(0)
            get_pid(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value == int(pid) and user32.IsWindowVisible(hwnd):
                # SW_HIDE = 0. Do not close the window: the converter must keep running.
                show_window(hwnd, 0)
            return True

        enum_windows(_enum, 0)
    except Exception:
        # Hiding is best-effort; conversion and its error handling must continue.
        return


def _run_oda_converter_hidden(cmd, timeout=180):
    """Run ODA's command-line conversion without leaving its GUI visible."""
    startupinfo = None
    creationflags = 0
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    # File-backed logs avoid pipe-buffer deadlocks while the process is polled
    # to hide the ODA GUI window as soon as it is created.
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8', errors='replace') as stdout_log, \
         tempfile.TemporaryFile(mode='w+t', encoding='utf-8', errors='replace') as stderr_log:
        try:
            proc = subprocess.Popen(
                cmd, stdout=stdout_log, stderr=stderr_log, text=True,
                startupinfo=startupinfo, creationflags=creationflags,
                stdin=subprocess.DEVNULL, close_fds=True,
            )
        except OSError as exc:
            raise RuntimeError(f'Nie można uruchomić ODA File Converter: {exc}') from exc

        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if os.name == 'nt':
                _hide_windows_for_process(proc.pid)
            if time.monotonic() >= deadline:
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.wait()
                raise RuntimeError(f'ODA File Converter przekroczył limit {timeout} s.')
            time.sleep(0.08)

        stdout_log.seek(0)
        stderr_log.seek(0)
        return subprocess.CompletedProcess(
            args=cmd, returncode=proc.returncode,
            stdout=stdout_log.read(), stderr=stderr_log.read(),
        )


def auto_convert_dwg_to_dxf(name, data, audit=True):
    """Convert one DWG to DXF through an installed ODA File Converter.

    The converter is detected automatically; no path configuration is required.
    The ODA executable is intentionally not bundled with Thanos.
    """
    converter = find_oda_converter()
    if not converter:
        raise FileNotFoundError(
            'Nie znaleziono ODA File Converter. Zainstaluj ODA File Converter '
            'lub dodaj ODAFileConverter.exe do PATH.'
        )

    with tempfile.TemporaryDirectory(prefix='thanos_dwg_') as work:
        root = Path(work)
        src = root / 'input'
        dst = root / 'output'
        src.mkdir(); dst.mkdir()
        safe_name = Path(name).name
        if Path(safe_name).suffix.lower() != '.dwg':
            safe_name += '.dwg'
        source = src / safe_name
        source.write_bytes(data)

        # ODA File Converter accepts: input_dir output_dir version format recurse audit [filter].
        cmd = [converter, str(src), str(dst), 'ACAD2018', 'DXF', '0', '1' if audit else '0', '*.DWG']
        proc = _run_oda_converter_hidden(cmd, timeout=180)

        out_path = dst / (Path(safe_name).stem + '.dxf')
        if not out_path.exists():
            # Some ODA builds may preserve case or use a slightly different name.
            candidates = list(dst.glob('*.dxf')) + list(dst.glob('*.DXF'))
            if candidates:
                out_path = candidates[0]
        if not out_path.exists():
            details = ' '.join(x.strip() for x in (proc.stdout or '', proc.stderr or '') if x.strip())
            raise RuntimeError(
                f'ODA nie utworzył pliku DXF (kod {proc.returncode}).'
                + (f' Komunikat: {details[:600]}' if details else '')
            )
        return out_path.read_bytes(), converter


def get_dxf_files_with_auto_conversion(files, auto=True):
    """Return original DXFs plus DWGs converted transparently to in-memory DXF files."""
    result = []
    converted = st.session_state.setdefault('converted_dxf', {})
    messages = []
    for f in files or []:
        ext = Path(f.name).suffix.lower()
        if ext == '.dxf':
            result.append(f)
            continue
        if ext != '.dwg' or not auto:
            continue
        cache_key = _file_content_cache_key(f)
        data = converted.get(cache_key)
        if data is None:
            try:
                data, converter = auto_convert_dwg_to_dxf(f.name, f.getvalue(), audit=True)
                converted[cache_key] = data
                messages.append((f.name, True, f'Automatyczna konwersja DWG → DXF przez {Path(converter).parent}'))
            except Exception as exc:
                messages.append((f.name, False, f'{type(exc).__name__}: {exc}'))
                continue
        result.append(_MemoryFile(
            Path(f.name).stem + '.dxf', data,
            display_name=f'{f.name}  →  DXF (automatycznie)',
            source_name=f.name,
        ))
    return result, messages


def _xy(entity):
    try:
        ins = entity.dxf.insert
        return float(ins.x), float(ins.y)
    except Exception:
        return None


def _text_value(entity):
    try:
        t = entity.dxftype()
        if t == 'TEXT':
            return str(entity.dxf.text or '')
        if t == 'MTEXT':
            return str(entity.text or '')
    except Exception:
        return ''
    return ''


def _insert_attributes(entity):
    attrs = {}
    try:
        for a in getattr(entity, 'attribs', []) or []:
            tag = str(getattr(a.dxf, 'tag', '') or '').strip().upper()
            val = str(getattr(a.dxf, 'text', '') or '').strip()
            if tag:
                attrs[tag] = val
    except Exception:
        pass
    return attrs


def _dynamic_true_name_map(data: bytes):
    """Resolve anonymous AutoCAD dynamic-block names to their real design names.

    AutoCAD commonly stores a dynamic block reference as an anonymous name such
    as ``*U840``.  The corresponding ``BLOCK_RECORD`` contains an
    ``AcDbBlockRepBTag`` entry whose group-code 1005 points to the handle of the
    real/named ``BLOCK_RECORD`` (for example ``*U840 -> optyczna czujka dymu``).

    Some drawings also expose ``AcDbDynamicBlockTrueName`` directly.  Both forms
    are handled here.  The handle relationship is important because merely
    reading ``AcDbDynamicBlockTrueName`` from named records does *not* tell us
    which anonymous ``*Uxxx`` records belong to them.
    """
    mapping = {}
    try:
        text = data.decode('utf-8', errors='ignore')
        lines = [x.strip() for x in text.splitlines()]

        # First pass: collect BLOCK_RECORD handle -> name and, for each record,
        # the dynamic-block TrueName and AcDbBlockRepBTag target handle.
        records = {}
        i = 0
        n = len(lines)
        while i + 1 < n:
            code, value = lines[i], lines[i + 1]
            if code == '0' and value == 'BLOCK_RECORD':
                rec = {'handle': '', 'name': '', 'true_name': '', 'btag': ''}
                i += 2
                while i + 1 < n:
                    c, v = lines[i], lines[i + 1]
                    if c == '0':
                        break
                    if c == '5' and not rec['handle']:
                        rec['handle'] = v
                    elif c == '2' and not rec['name']:
                        rec['name'] = v
                    elif c == '1001' and v == 'AcDbDynamicBlockTrueName':
                        # The next 1000 value is the actual design name.
                        j = i + 2
                        while j + 1 < n and lines[j] != '0':
                            if lines[j] == '1000':
                                rec['true_name'] = lines[j + 1].strip()
                                break
                            j += 2
                    elif c == '1001' and v == 'AcDbBlockRepBTag':
                        # The target is stored as group code 1005.
                        j = i + 2
                        while j + 1 < n and lines[j] != '0':
                            if lines[j] == '1005':
                                rec['btag'] = lines[j + 1].strip()
                                break
                            j += 2
                    i += 2
                if rec['handle']:
                    records[rec['handle'].upper()] = rec
                continue
            i += 2

        # Direct TrueName information is useful when the record itself carries
        # it, but the BTag relationship is what resolves anonymous *Uxxx names.
        for rec in records.values():
            name = str(rec.get('name') or '').strip()
            true_name = str(rec.get('true_name') or '').strip()
            if name and true_name and name != true_name:
                mapping[name] = true_name

        # Anonymous BLOCK_RECORD -> named BLOCK_RECORD via AcDbBlockRepBTag.
        for rec in records.values():
            name = str(rec.get('name') or '').strip()
            target = records.get(str(rec.get('btag') or '').upper())
            if not name or not target:
                continue
            target_name = str(target.get('true_name') or target.get('name') or '').strip()
            if not target_name:
                continue
            if name.startswith(('*', 'G$C', 'A$C')):
                mapping[name] = target_name

    except Exception:
        pass
    return mapping

def _dynamic_alias_map_from_blocks(doc):
    """Infer anonymous dynamic-block aliases from their ATTDEF signatures.

    AutoCAD may create many *Uxxx variants of one named dynamic block.  In some
    DXFs the anonymous BLOCK_RECORD has no directly readable TrueName link.  A
    stable fallback is the set of ATTDEF tags/default texts inside the block;
    e.g. OPIS_CCTV=CAM uniquely identifies IE_TT_KAMERA_1 in many project files.
    """
    try:
        named = {}
        for b in doc.blocks:
            n = str(b.name or '').strip()
            if not n or n.startswith('*') or n.startswith('G$C') or n.startswith('A$C'):
                continue
            sig = []
            for e in b:
                if e.dxftype() == 'ATTDEF':
                    sig.append((str(getattr(e.dxf, 'tag', '') or '').strip().upper(),
                                str(getattr(e.dxf, 'text', '') or '').strip()))
            if sig:
                key = tuple(sorted(sig))
                named.setdefault(key, []).append(n)
        aliases = {}
        for b in doc.blocks:
            n = str(b.name or '').strip()
            if not (n.startswith('*') or n.startswith('G$C') or n.startswith('A$C')):
                continue
            sig = []
            for e in b:
                if e.dxftype() == 'ATTDEF':
                    sig.append((str(getattr(e.dxf, 'tag', '') or '').strip().upper(),
                                str(getattr(e.dxf, 'text', '') or '').strip()))
            if sig:
                key = tuple(sorted(sig))
                candidates = named.get(key, [])
                if len(candidates) == 1:
                    aliases[n] = candidates[0]
        return aliases
    except Exception:
        return {}


def _resolved_block_name(entity, block_name, true_name_map):
    """Return the design/true name for dynamic or anonymous blocks."""
    return str(true_name_map.get(block_name, block_name) or block_name).strip()


def _element_attributes(entity, block_name):
    """Extract the design-facing attributes used for counting.

    Attribute tags are matched case-insensitively. New projects often use
    ``Misc`` and ``Name`` while older ones use ``NAZWA``/``TYP``.
    """
    attrs = _insert_attributes(entity)
    misc = attrs.get('MISC', '').strip()
    name = attrs.get('NAME', '').strip()
    legacy_name = ''
    for key in ('NAZWA', 'TYP', 'TYPE', 'SYMBOL', 'OPIS'):
        val = attrs.get(key, '').strip()
        if val:
            legacy_name = val
            break
    # For identity, prefer explicit Name/Misc. Dynamic access-point blocks are
    # a special case: TYP commonly stores the hardware model (e.g. AP-535),
    # not the element identity. Keep the resolved CAD block name as the item
    # name and retain TYP separately in the raw attributes for details/export.
    resolved_identity = str(block_name or '').strip()
    if resolved_identity.casefold() == 'access_point':
        element_name = name or misc or resolved_identity
    else:
        # For other blocks, prefer Name, then Misc, then legacy naming
        # attributes, and finally the technical CAD block name.
        element_name = name or misc or legacy_name or block_name
    return misc, name, element_name


def _element_name(entity, block_name):
    return _element_attributes(entity, block_name)[2]


def parse_dxf_blocks(data: bytes, exclude_legend: bool = True, legend_radius_factor: float = 0.04, count_mode: str = 'Wg bloków CAD'):
    """Count INSERT references, resolving AutoCAD dynamic-block True Names."""
    import ezdxf
    with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
        tmp.write(data)
        p = Path(tmp.name)
    try:
        doc = ezdxf.readfile(str(p))
        true_name_map = _dynamic_true_name_map(data)
        true_name_map.update(_dynamic_alias_map_from_blocks(doc))
        rows = []
        for layout in (doc.modelspace(),):
            inserts = []
            points = []
            legend_points = []
            for entity in layout:
                et = entity.dxftype()
                if et == 'INSERT':
                    xy = _xy(entity)
                    if xy:
                        points.append(xy)
                    block_name = str(getattr(entity.dxf, 'name', '') or 'UNKNOWN').strip() or 'UNKNOWN'
                    resolved_name = _resolved_block_name(entity, block_name, true_name_map)
                    misc, name_attr, element_name = _element_attributes(entity, resolved_name)
                    # XREF / odnośniki zewnętrzne nie są elementami instalacji i nie mogą
                    # trafiać do zestawienia ilościowego.
                    identity_text = ' '.join(str(v or '') for v in (block_name, resolved_name, misc, name_attr, element_name)).casefold()
                    if any(tok in identity_text for tok in ('xref_', 'xref-', 'xref ', 'xref\\', '*xref')):
                        continue
                    inserts.append((entity, block_name, resolved_name, misc, name_attr, element_name, xy))
                elif exclude_legend and et in ('TEXT', 'MTEXT'):
                    txt = re.sub(r'\s+', ' ', _text_value(entity)).strip().upper()
                    if re.search(r'\b(LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE)\b', txt):
                        xy = _xy(entity)
                        if xy:
                            legend_points.append(xy)

            if points:
                xs = [p[0] for p in points]; ys = [p[1] for p in points]
                diag = max(((max(xs)-min(xs))**2 + (max(ys)-min(ys))**2) ** 0.5, 1.0)
            else:
                diag = 1.0
            # Identify the *actual INSERT entities* belonging to each legend.
            # Prefer the geometric relationship used by a CAD legend table:
            # a symbol INSERT is placed immediately to the left of its TEXT/MTEXT
            # description.  This is much safer than growing a free-form cluster,
            # which can accidentally absorb real plan symbols located nearby.
            legend_entity_ids = set()
            for lx, ly in legend_points:
                local_texts = []
                # Collect description rows below the heading.  Stop only when a
                # large vertical gap appears; this keeps the whole legend together
                # while excluding unrelated notes further down the drawing.
                candidates_text = []
                for ln, (tx, ty, txt, layer) in []:
                    pass
                # We do not have the global TEXT collection here, so inspect the
                # current layout again for text rows close to the heading.
                heading_rows = []
                try:
                    for e in layout:
                        if e.dxftype() not in ('TEXT', 'MTEXT'):
                            continue
                        xy = _xy(e)
                        if not xy:
                            continue
                        tx, ty = xy
                        txt = re.sub(r'\s+', ' ', _text_value(e)).strip()
                        if not txt or re.search(r'\b(LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE)\b', txt, re.I):
                            continue
                        # Legend descriptions are to the right of the heading and
                        # normally vertically below it.  Keep a generous local box.
                        if -250 <= tx - lx <= 900 and -1100 <= ty - ly <= 80:
                            if re.search(r'[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]', txt):
                                heading_rows.append((tx, ty, txt))
                except Exception:
                    heading_rows = []

                if not heading_rows:
                    continue

                # Determine the actual legend row band from the description texts.
                min_ty = min(t[1] for t in heading_rows)
                max_ty = max(t[1] for t in heading_rows)
                min_tx = min(t[0] for t in heading_rows)
                max_tx = max(t[0] for t in heading_rows)
                # Symbols are normally just left of the description column.
                x_left = min_tx - 260
                x_right = min_tx + 120
                y_bottom = min_ty - 90
                y_top = max_ty + 90

                # Match each INSERT to the nearest description row on its right.
                # A row may contain multiple nested/dynamic INSERTs, therefore all
                # inserts satisfying the tight row relationship are selected.
                for idx, rec in enumerate(inserts):
                    exy = rec[-1]
                    if not exy:
                        continue
                    ex, ey = exy
                    if str(rec[4]) == 'SSP_CZUJKA_T' or 'Optyczno-termiczna' in str(rec[5]) or str(rec[5]).lower().startswith('optyczna czujka'):
                        print('CHECK',rec[1],rec[4],rec[5],ex,ey,'bounds',x_left,x_right,y_bottom,y_top)
                    if not (x_left <= ex <= x_right and y_bottom <= ey <= y_top):
                        continue
                    row_match = False
                    for tx, ty, txt in heading_rows:
                        # Symbol left of label, same row.  Allow multiline text.
                        if tx >= ex and (tx - ex) <= 500 and abs(ty - ey) <= 85:
                            row_match = True
                            break
                    if row_match:
                        legend_entity_ids.add(id(rec[0]))

                # If no symbols were paired, use the nearest INSERT per text row as
                # a conservative fallback. This still avoids the old broad cluster.
                if not any(id(rec[0]) in legend_entity_ids for rec in inserts):
                    for tx, ty, txt in heading_rows:
                        nearest = []
                        for rec in inserts:
                            exy = rec[-1]
                            if not exy:
                                continue
                            ex, ey = exy
                            if ex > tx or tx - ex > 500 or abs(ey - ty) > 100:
                                continue
                            nearest.append((math.hypot(tx-ex, ty-ey), rec))
                        if nearest:
                            nearest.sort(key=lambda z: z[0])
                            legend_entity_ids.add(id(nearest[0][1][0]))

            def in_legend_area(entity_or_xy):
                if not exclude_legend:
                    return False
                # Kept as a compatibility helper; exclusion is entity-based.
                if isinstance(entity_or_xy, tuple) and len(entity_or_xy) == 2:
                    return False
                return id(entity_or_xy) in legend_entity_ids

            # Build a dynamic dictionary ONLY from blocks actually present in the legend.
            # The dictionary is based on the exact INSERT entities selected above.
            legend_identities = set()
            for entity, block_name, resolved_name, misc, name_attr, element_name, xy in inserts:
                if id(entity) in legend_entity_ids:
                    for ident in (element_name, name_attr, misc, resolved_name, block_name):
                        ident = str(ident or '').strip()
                        if ident:
                            legend_identities.add(ident.casefold())

            excluded = 0
            for entity, block_name, resolved_name, misc, name_attr, element_name, xy in inserts:
                in_legend = id(entity) in legend_entity_ids
                if in_legend:
                    print('EXCL', element_name, block_name, xy)
                    excluded += 1
                    continue
                # In CAD mode every INSERT can be counted, including anonymous G$C/*U names.
                # In legend mode, identity is constrained by the dynamic legend dictionary.
                if not element_name:
                    continue
                # In legend-graphic mode, only count elements that have a matching graphic
                # representation in the detected legend. The graphic is identified by the
                # same design identity (NAZWA/NAME/TYP/... or, as a fallback, block name).
                if count_mode == 'Wg grafik na legendzie':
                    # Match against the dynamic legend dictionary using all available
                    # identifiers. This is important for DXF files where the INSERT name
                    # is anonymous but Name/Misc carries the actual project designation.
                    candidates = {str(v or '').strip().casefold() for v in (element_name, name_attr, misc, resolved_name, block_name) if str(v or '').strip()}
                    if not candidates.intersection(legend_identities):
                        continue
                rows.append({
                    'symbol': block_name,
                    'resolved_block_name': resolved_name,
                    'misc': misc,
                    'name_attr': name_attr,
                    'element_name': element_name,
                    'kind': 'BLOCK',
                    'layout': layout.name,
                    'handle': str(getattr(entity.dxf, 'handle', '') or ''),
                })
            if excluded:
                rows.append({'symbol': '__LEGEND_EXCLUDED__', 'element_name': '', 'kind': 'META', 'layout': f'{layout.name}: {excluded}'})
        return pd.DataFrame(rows, columns=['symbol', 'resolved_block_name', 'misc', 'name_attr', 'element_name', 'kind', 'layout', 'handle'])
    finally:
        p.unlink(missing_ok=True)




def _clean_legend_label(label):
    """Normalize a legend description without changing the actual model name."""
    text = re.sub(r'\s+', ' ', str(label or '')).strip()
    # Remove common CAD legend separators/bullets at the beginning/end.
    text = re.sub(r'^\s*[-–—•·:;|]+\s*', '', text)
    text = re.sub(r'\s*[-–—•·:;|]+\s*$', '', text)
    # Remove repeated leading numbering such as "1. ", "01 - ", "(1) ".
    text = re.sub(r'^\s*\(?\d{1,3}\)?\s*[\.\-–—:]\s*', '', text)
    return text.strip()


def _legend_display_name_map(data: bytes, exclude_legend: bool = True):
    """Read complete symbol descriptions from the actual legend table."""
    try:
        import ezdxf
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(data); path=Path(tmp.name)
        doc=ezdxf.readfile(path)

        inserts=[]; texts=[]; heads=[]
        # Build the dynamic-block map ONCE per DXF. Rebuilding it for every INSERT
        # is extremely expensive on large drawings because it scans the complete
        # DXF text (millions of lines) each time.
        tm=_dynamic_true_name_map(data)
        for layout in (doc.modelspace(),):
            for e in layout:
                if e.dxftype()=='INSERT':
                    xy=_xy(e)
                    if xy:
                        bn=str(getattr(e.dxf,'name','') or '').strip()
                        resolved=_resolved_block_name(e,bn,tm)
                        misc,name_attr,element=_element_attributes(e,resolved)
                        inserts.append((layout.name,e,xy,bn,resolved,misc,name_attr,element))
                elif e.dxftype() in ('TEXT','MTEXT'):
                    xy=_xy(e); txt=re.sub(r'\s+',' ',_text_value(e)).strip()
                    if xy and txt:
                        layer=str(getattr(e.dxf,'layer','') or '')
                        texts.append((layout.name,xy,txt,layer))
                        if re.search(r'\b(LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE)\b',txt,re.I):
                            heads.append((layout.name,xy))

        # Drawing scale.
        xyall=[r[2] for r in inserts]
        diag=max(math.hypot(max(x for x,y in xyall)-min(x for x,y in xyall),
                            max(y for x,y in xyall)-min(y for x,y in xyall)),1.0) if xyall else 1.0

        # Identify the actual legend description area. Prefer text on layers named
        # "opis"/"legenda" located near a LEGENDA/OZNACZENIA heading.
        legend_texts=[]
        for t in texts:
            lname=t[3].upper()
            is_desc_layer=any(k in lname for k in ('OPIS','LEGENDA','LEGEND','OZNAC'))
            if not is_desc_layer:
                continue
            if heads:
                d=min(math.dist(t[1],hxy) for hname,hxy in heads if hname==t[0]) if any(hn==t[0] for hn,_ in heads) else 1e99
                if d>diag*0.18:
                    continue
            if re.fullmatch(r'x\s*\d+',t[2],re.I):
                continue
            legend_texts.append(t)

        # If no heading was detected, fall back to all description-layer texts.
        if not legend_texts:
            legend_texts=[t for t in texts if any(k in t[3].upper() for k in ('OPIS','LEGENDA','LEGEND','OZNAC'))
                          and not re.fullmatch(r'x\s*\d+',t[2],re.I)]
        # Some drawings put legend descriptions on a generic layer. If a legend
        # heading was found but no description-layer text was found, use text near
        # that heading as a controlled fallback.
        if not legend_texts and heads:
            for t in texts:
                if re.fullmatch(r'x\s*\d+',t[2],re.I):
                    continue
                if re.search(r'\b(LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE)\b',t[2],re.I):
                    continue
                if any(t[0]==hn and math.dist(t[1],hxy) <= diag*0.18 for hn,hxy in heads):
                    legend_texts.append(t)

        def clean(txt):
            return _clean_legend_label(txt)

        mapping={}
        # Sort symbol rows from top to bottom. For duplicate block names,
        # the first matching legend row is the one used.
        ordered=sorted(inserts,key=lambda r:(r[0],-r[2][1],r[2][0]))

        for layout,e,xy,bn,resolved,misc,name_attr,element in ordered:
            candidates=[]
            for ln,txy,txt,layer in legend_texts:
                if ln!=layout:
                    continue
                dx=txy[0]-xy[0]; dy=txy[1]-xy[1]
                if dx<0 or dx>4500 or abs(dy)>750:
                    continue
                label=clean(txt)
                if not label or re.search(r'\b(LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE)\b',label,re.I):
                    continue
                if not re.search(r'[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]',label):
                    continue
                candidates.append((abs(dy),dx,txy[1],label))

            if not candidates:
                continue

            # Pick the closest line, then collect adjacent lines belonging to
            # the same legend entry (multi-line descriptions).
            best=min(candidates,key=lambda c:(c[0],c[1]))
            group=[c for c in candidates if abs(c[2]-best[2])<=520 and abs(c[1]-best[1])<=400]
            group=sorted(group,key=lambda c:-c[2])
            parts=[]
            for c in group:
                if c[3] not in parts:
                    parts.append(c[3])
            label=' '.join(parts)
            ids=[name_attr,misc,element,resolved,bn]
            for ident in ids:
                key=re.sub(r'\s+',' ',str(ident or '')).strip().casefold()
                if key:
                    mapping.setdefault(key,label)

        return mapping
    except Exception:
        return {}
    finally:
        try: path.unlink(missing_ok=True)
        except Exception: pass


def measure_odgrom_uziom(name, data, unit_scale=0.001, selected_layers=None):
    """Pomiar instalacji odgromowej/uziomu z DXF.
    Mierzy LINE/LWPOLYLINE/POLYLINE i grupuje wg warstwy oraz rozpoznanej kategorii.
    Dodatkowo zlicza wystąpienia charakterystycznych bloków osprzętu.
    """
    try:
        import tempfile, ezdxf, pandas as pd
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(data)
            path=Path(tmp.name)
        doc=ezdxf.readfile(path)
        rows=[]
        def seglen(x1,y1,x2,y2,bulge=0.0):
            chord=math.hypot(x2-x1,y2-y1)
            if abs(bulge)<1e-12: return chord
            theta=4*math.atan(abs(bulge))
            ss=math.sin(theta/2)
            if abs(ss)<1e-12: return chord
            return (chord/(2*ss))*theta
        def category(layer):
            u=layer.upper()
            if 'BEDNARK' in u or 'UZIO' in u:
                return 'Uziom / bednarka'
            if '2DE' in u or 'ODGROM' in u or 'LPS' in u:
                return 'Przewód odgromowy'
            if 'ODST' in u:
                return 'Odstępy izolacyjne'
            return 'Pozostałe'
        for layout in (doc.modelspace(),):
            for e in layout:
                et=e.dxftype()
                layer=str(getattr(e.dxf,'layer','') or '').strip()
                if selected_layers and layer not in selected_layers: continue
                length=0.0
                if et=='LINE':
                    a,b=e.dxf.start,e.dxf.end
                    length=math.dist((a.x,a.y,a.z),(b.x,b.y,b.z))
                elif et=='LWPOLYLINE':
                    pts=list(e.get_points('xyb'))
                    for i in range(len(pts)-1):
                        length+=seglen(pts[i][0],pts[i][1],pts[i+1][0],pts[i+1][1],
                                       pts[i][2] if len(pts[i])>2 else 0.0)
                    if e.closed and len(pts)>2:
                        length+=seglen(pts[-1][0],pts[-1][1],pts[0][0],pts[0][1],
                                       pts[-1][2] if len(pts[-1])>2 else 0.0)
                elif et=='POLYLINE':
                    verts=list(e.vertices)
                    for a,b in zip(verts,verts[1:]):
                        p,q=a.dxf.location,b.dxf.location
                        length+=math.dist((p.x,p.y,p.z),(q.x,q.y,q.z))
                    if getattr(e.dxf,'flags',0)&1 and len(verts)>2:
                        p,q=verts[-1].dxf.location,verts[0].dxf.location
                        length+=math.dist((p.x,p.y,p.z),(q.x,q.y,q.z))
                if length>1e-9:
                    rows.append({
                        'Plik':name,'Arkusz':layout.name,'Kategoria':category(layer),
                        'Warstwa':layer,'Długość [m]':length*unit_scale,
                        'Obiekty':1,'Typ geometrii':et
                    })
        df=pd.DataFrame(rows)
        if df.empty:
            return df, pd.DataFrame()
        geom=df.groupby(['Plik','Arkusz','Kategoria','Warstwa'],as_index=False).agg(
            **{'Długość [m]':('Długość [m]','sum'),'Obiekty':('Obiekty','sum'),
               'Typ geometrii':('Typ geometrii',lambda x:', '.join(sorted(set(x))))})
        # Block counts for typical lightning protection/earthing accessories.
        b_rows=[]
        for layout in (doc.modelspace(),):
            for e in layout:
                if e.dxftype()!='INSERT': continue
                bn=str(getattr(e.dxf,'name','') or '').strip()
                if bn in {'zejscie','WYPROWADZENIE_MIEJSCOWE SZYNY',
                          '2d_elko_endsym_og012_f9y089o','Pd_hardt_z1_ah1603og_5hjrx0s',
                          'Pd_elko_bis_nx198_pfevay'}:
                    display=bn
                    attrs={}
                    try:
                        for a in e.attribs:
                            tag=str(a.dxf.tag).upper()
                            if tag in ('NAME','ARTICLE','PRODUCER'):
                                attrs[tag]=str(a.dxf.text).strip()
                    except Exception: pass
                    if attrs.get('NAME'): display=attrs['NAME']
                    elif bn=='zejscie': display='Zejście przewodu odgromowego'
                    b_rows.append({'Plik':name,'Arkusz':layout.name,'Element':display,'Blok CAD':bn,'Ilość':1})
        bd=pd.DataFrame(b_rows)
        if not bd.empty:
            bd=bd.groupby(['Plik','Arkusz','Element','Blok CAD'],as_index=False)['Ilość'].sum()
        return geom, bd
    except Exception as exc:
        try: st.session_state['odg_error']=f"{type(exc).__name__}: {exc}"
        except Exception: pass
        return pd.DataFrame(), pd.DataFrame()
    finally:
        try: path.unlink(missing_ok=True)
        except Exception: pass



@st.cache_data(show_spinner=False, max_entries=32)
def _extract_route_geometry(data: bytes):
    """Parse DXF route geometry once and keep the result in Streamlit cache.

    The expensive ezdxf file parsing and geometry traversal are independent of
    selected layers and unit scale, so they are cached separately from the UI
    filters. This makes repeated measurements on the same DXF much faster.
    """
    import tempfile, ezdxf
    with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    try:
        doc = ezdxf.readfile(path)
        rows = []

        def segment_len(x1, y1, x2, y2, bulge=0.0):
            chord = math.hypot(x2-x1, y2-y1)
            if abs(bulge) < 1e-12:
                return chord
            theta = 4 * math.atan(abs(bulge))
            s = math.sin(theta / 2)
            if abs(s) < 1e-12:
                return chord
            return (chord / (2 * s)) * theta

        measurable_types = {'LINE', 'LWPOLYLINE', 'POLYLINE'}
        for layout in (doc.modelspace(),):
            layout_name = layout.name
            for entity in layout:
                et = entity.dxftype()
                if et not in measurable_types:
                    continue

                layer = str(getattr(entity.dxf, 'layer', '') or '').strip()
                length = 0.0
                width = None

                if et == 'LINE':
                    a, b = entity.dxf.start, entity.dxf.end
                    length = math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))

                elif et == 'LWPOLYLINE':
                    cw = float(getattr(entity.dxf, 'const_width', 0.0) or 0.0)
                    if cw > 0:
                        width = cw
                    # get_points is substantially cheaper than constructing
                    # individual DXF entities for every segment.
                    pts = entity.get_points('xyb')
                    if len(pts) >= 2:
                        prev = pts[0]
                        for cur in pts[1:]:
                            length += segment_len(
                                prev[0], prev[1], cur[0], cur[1],
                                prev[2] if len(prev) > 2 else 0.0
                            )
                            prev = cur
                        if entity.closed:
                            length += segment_len(
                                pts[-1][0], pts[-1][1], pts[0][0], pts[0][1],
                                pts[-1][2] if len(pts[-1]) > 2 else 0.0
                            )

                else:  # POLYLINE
                    cw = float(getattr(entity.dxf, 'const_width', 0.0) or 0.0)
                    if cw > 0:
                        width = cw
                    verts = list(entity.vertices)
                    if len(verts) >= 2:
                        for a, b in zip(verts, verts[1:]):
                            p1, p2 = a.dxf.location, b.dxf.location
                            length += math.dist(
                                (p1.x, p1.y, p1.z),
                                (p2.x, p2.y, p2.z)
                            )
                        if getattr(entity.dxf, 'flags', 0) & 1 and len(verts) > 2:
                            p1, p2 = verts[-1].dxf.location, verts[0].dxf.location
                            length += math.dist(
                                (p1.x, p1.y, p1.z),
                                (p2.x, p2.y, p2.z)
                            )

                if length > 1e-9:
                    rows.append((layout_name, layer, et, length, width))

        return rows
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


@st.cache_data(show_spinner=False, max_entries=32)
def measurable_dxf_layers(data: bytes):
    """Return measurable DXF layers using the same cached geometry scan."""
    return tuple(sorted({
        row[1] for row in _extract_route_geometry(data)
        if row[1]
    }))


def measure_dxf_lengths(name, data, unit_scale=0.001, selected_layers=None):
    """Measure route geometry and identify tray width from DXF geometry.

    For LWPOLYLINE the DXF const_width value is used as the tray width in
    drawing units. In the supplied project this is 100, 200 or 300 mm.
    """
    try:
        import tempfile, ezdxf, pandas as pd
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(data)
            path = Path(tmp.name)
        doc = ezdxf.readfile(path)
        rows = []

        def segment_len(x1, y1, x2, y2, bulge=0.0):
            chord = math.hypot(x2-x1, y2-y1)
            if abs(bulge) < 1e-12:
                return chord
            theta = 4 * math.atan(abs(bulge))
            s = math.sin(theta/2)
            if abs(s) < 1e-12:
                return chord
            radius = chord / (2*s)
            return radius * theta

        for layout in (doc.modelspace(),):
            for entity in layout:
                et = entity.dxftype()
                layer = str(getattr(entity.dxf, 'layer', '') or '').strip()
                if selected_layers and layer not in selected_layers:
                    continue

                length = 0.0
                width = None

                if et == 'LINE':
                    a, b = entity.dxf.start, entity.dxf.end
                    length = math.dist((a.x,a.y,a.z), (b.x,b.y,b.z))

                elif et == 'LWPOLYLINE':
                    # Critical for cable trays: AutoCAD stores tray width here.
                    cw = float(getattr(entity.dxf, 'const_width', 0.0) or 0.0)
                    if cw > 0:
                        width = cw
                    pts = list(entity.get_points('xyb'))
                    if len(pts) >= 2:
                        for i in range(len(pts)-1):
                            x1,y1 = pts[i][0], pts[i][1]
                            x2,y2 = pts[i+1][0], pts[i+1][1]
                            bulge = pts[i][2] if len(pts[i]) > 2 else 0.0
                            length += segment_len(x1,y1,x2,y2,bulge)
                        if entity.closed:
                            x1,y1 = pts[-1][0],pts[-1][1]
                            x2,y2 = pts[0][0],pts[0][1]
                            bulge = pts[-1][2] if len(pts[-1]) > 2 else 0.0
                            length += segment_len(x1,y1,x2,y2,bulge)

                elif et == 'POLYLINE':
                    # Some old drawings store width on vertices or the polyline.
                    cw = float(getattr(entity.dxf, 'const_width', 0.0) or 0.0)
                    if cw > 0:
                        width = cw
                    verts = list(entity.vertices)
                    if len(verts) >= 2:
                        for a,b in zip(verts,verts[1:]):
                            p1,p2 = a.dxf.location,b.dxf.location
                            length += math.dist((p1.x,p1.y,p1.z),(p2.x,p2.y,p2.z))
                        if getattr(entity.dxf,'flags',0) & 1 and len(verts)>2:
                            p1,p2=verts[-1].dxf.location,verts[0].dxf.location
                            length += math.dist((p1.x,p1.y,p1.z),(p2.x,p2.y,p2.z))

                if length > 1e-9:
                    width_mm = None
                    if width is not None:
                        width_mm = width * unit_scale * 1000.0
                        # Keep common tray widths clean: 99.999 -> 100.
                        if abs(width_mm-round(width_mm)) < 0.01:
                            width_mm = int(round(width_mm))
                    tray = f"K{width_mm}" if width_mm else "Nie określono"

                    rows.append({
                        'Plik': name,
                        'Arkusz': layout.name,
                        'Warstwa': layer,
                        'Koryto': tray,
                        'Szerokość [mm]': width_mm if width_mm is not None else '',
                        'Długość [m]': length * unit_scale,
                        'Obiekty': 1,
                        'Typ geometrii': et
                    })

        df = pd.DataFrame(rows)
        if df.empty:
            return df

        return df.groupby(
            ['Plik','Arkusz','Warstwa','Koryto','Szerokość [mm]'],
            as_index=False
        ).agg(
            **{
                'Długość [m]': ('Długość [m]', 'sum'),
                'Obiekty': ('Obiekty', 'sum'),
                'Typ geometrii': ('Typ geometrii', lambda x: ', '.join(sorted(set(x))))
            }
        )
    except Exception as exc:
        try:
            st.session_state['measure_error'] = f"{type(exc).__name__}: {exc}"
        except Exception:
            pass
        return pd.DataFrame()
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def analyze_dxf(name, data, patterns, exclude_legend=True, count_mode='Wg bloków CAD'):
    df = parse_dxf_blocks(data, exclude_legend=exclude_legend, count_mode=count_mode)
    meta = df[df['kind'] == 'META'].copy() if not df.empty else pd.DataFrame()
    df = df[df['kind'] == 'BLOCK'].copy() if not df.empty else df
    if df.empty:
        return [], meta
    # Human-readable display name comes from the actual legend, never from a
    # predefined dictionary. Model identity remains available in Name/Misc.
    legend_names = _legend_display_name_map(data, exclude_legend=exclude_legend)
    rows = []
    group_cols = ['layout', 'misc', 'name_attr', 'element_name']
    for (layout, misc, name_attr, element_name), grp in df.groupby(group_cols, dropna=False):
        n = len(grp)
        # Keep one technical block name for traceability when multiple anonymous block names map to one element.
        block_names = sorted(set(str(x) for x in grp['symbol'].tolist() if str(x).strip()))
        technical_name = ', '.join(block_names[:5]) + (' …' if len(block_names) > 5 else '')
        # Keep a concrete INSERT handle for the preview. This makes the preview
        # show the actual selected CAD instance instead of the first INSERT with
        # the same block name elsewhere in the drawing.
        preview_handle = str(grp.iloc[0].get('handle', '') or '')
        typ_source = str(name_attr or misc or element_name)
        typ = classify(typ_source, patterns)
        # Resolve the user-facing name from the legend. Match by model Name,
        # Misc, resolved/technical block name, and finally element identity.
        model_keys = [str(v or '').strip() for v in (name_attr, misc, element_name, technical_name) if str(v or '').strip()]
        legend_display_name = next(
            (legend_names.get(re.sub(r'\s+', ' ', k).strip().casefold(), '')
             for k in model_keys
             if re.sub(r'\s+', ' ', k).strip().casefold() in legend_names),
            ''
        )
        display_name = str(element_name)
        rows.append({
            'Plik': name,
            'Strona': layout,
            'Typ elementu': typ,
            'Nazwa bloku': technical_name,
            'Misc': str(misc or ''),
            'Name': str(name_attr or ''),
            'Nazwa elementu': str(display_name),
            'Nazwa z legendy': str(legend_display_name),
            'Symbol/etykieta': str(name_attr or misc or element_name),
            'Ilość': int(n),
            'Metoda': ('Automatycznie / grafiki z legendy' if count_mode == 'Wg grafik na legendzie' else 'Automatycznie / bloki CAD + atrybuty Misc/Name'),
            'Pewność': 'wysoka',
            'Źródło': 'DXF/INSERT',
            '_PreviewHandle': preview_handle,
            '_PreviewLayout': str(layout or ''),
        })
    return rows, meta


def _legend_insert_info(data: bytes, exclude_legend: bool = True, legend_radius_factor: float = 0.04):
    """Returns legend block references and their coordinates from DXF."""
    try:
        import tempfile, ezdxf
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(data); path = Path(tmp.name)
        doc = ezdxf.readfile(path)
        infos = []
        for layout in (doc.modelspace(),):
            inserts = []
            legend_points = []
            points = []
            for entity in layout:
                et = entity.dxftype()
                if et == 'INSERT':
                    xy = _xy(entity)
                    if xy:
                        points.append(xy)
                    block_name = str(getattr(entity.dxf, 'name', '') or 'UNKNOWN').strip() or 'UNKNOWN'
                    inserts.append((entity, block_name, xy))
                elif et in ('TEXT', 'MTEXT'):
                    txt = re.sub(r'\s+', ' ', _text_value(entity)).strip().upper()
                    if re.search(r'\b(LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE)\b', txt):
                        xy = _xy(entity)
                        if xy:
                            legend_points.append(xy)
            if not legend_points or not inserts:
                continue
            if points:
                xs=[p[0] for p in points]; ys=[p[1] for p in points]
                diag=max(((max(xs)-min(xs))**2+(max(ys)-min(ys))**2)**0.5,1.0)
            else:
                diag=1.0
            radius=diag*legend_radius_factor
            for entity, block_name, xy in inserts:
                if xy and any(((xy[0]-lx)**2+(xy[1]-ly)**2)**0.5 <= radius for lx,ly in legend_points):
                    infos.append({'layout': layout.name, 'block_name': block_name, 'entity': entity, 'xy': xy})
        return doc, infos
    except Exception:
        return None, []
    finally:
        try: path.unlink(missing_ok=True)
        except Exception: pass


def _render_entity(ax, entity, transform=None):
    """Simple renderer for common DXF graphics from a block reference."""
    try:
        et = entity.dxftype()
        if et == 'LINE':
            a=entity.dxf.start; b=entity.dxf.end
            ax.plot([a.x,b.x],[a.y,b.y], linewidth=1.2)
        elif et == 'CIRCLE':
            c=entity.dxf.center; r=entity.dxf.radius
            import matplotlib.patches as patches
            ax.add_patch(patches.Circle((c.x,c.y),r,fill=False,linewidth=1.2))
        elif et == 'ARC':
            c=entity.dxf.center; r=entity.dxf.radius
            import matplotlib.patches as patches
            ax.add_patch(patches.Arc((c.x,c.y),2*r,2*r,angle=0,theta1=entity.dxf.start_angle,theta2=entity.dxf.end_angle,linewidth=1.2))
        elif et in ('LWPOLYLINE','POLYLINE'):
            pts=[]
            if et=='LWPOLYLINE':
                pts=[(p[0],p[1]) for p in entity.get_points('xy')]
                if entity.closed and pts: pts.append(pts[0])
            else:
                pts=[(v.dxf.location.x,v.dxf.location.y) for v in entity.vertices]
                if getattr(entity.dxf,'flags',0) & 1 and pts: pts.append(pts[0])
            if len(pts)>1:
                ax.plot([x for x,y in pts],[y for x,y in pts],linewidth=1.2)
        elif et == 'ELLIPSE':
            # fallback: sample parametric ellipse in WCS
            import numpy as np
            c=entity.dxf.center; major=entity.dxf.major_axis
            ratio=entity.dxf.ratio
            start=entity.dxf.start_param; end=entity.dxf.end_param
            vals=np.linspace(start,end,100)
            ax_x=c.x+major.x*np.cos(vals)-major.y*ratio*np.sin(vals)
            ax_y=c.y+major.y*np.cos(vals)+major.x*ratio*np.sin(vals)
            ax.plot(ax_x,ax_y,linewidth=1.2)
        elif et=='POINT':
            p=entity.dxf.location; ax.plot([p.x],[p.y],marker='o',markersize=3)
        elif et=='INSERT':
            for sub in entity.virtual_entities():
                _render_entity(ax, sub)
    except Exception:
        pass


def render_legend_screenshot_png(layout, insert_entity, center_xy=None, radius_factor=0.018, figsize=(2.4, 1.25), dpi=180):
    """Render a tight screenshot of ONE legend block reference.

    The image is built from the actual INSERT instance (virtual_entities), so the
    exported Excel gets a small raster equivalent of the exact CAD symbol, e.g.
    a PEL-1 tag with its green border/text. Nearby legend entries are NOT included.
    """
    if insert_entity is None:
        return None
    import numpy as np
    from ezdxf import bbox
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    try:
        # virtual_entities() contains the transformed graphics of this specific INSERT,
        # including text/geometry that belongs to the block definition.
        ents = list(insert_entity.virtual_entities())
        if not ents:
            return None

        # A block may be tiny or have zero-sized geometry; bbox.extents is more reliable
        # than a fixed radius because the screenshot should hug this one symbol.
        box = bbox.extents(ents, fast=True)
        if box.has_data is False:
            return None
        minx, miny = float(box.extmin.x), float(box.extmin.y)
        maxx, maxy = float(box.extmax.x), float(box.extmax.y)
        w = max(maxx - minx, 1e-6)
        h = max(maxy - miny, 1e-6)
        pad = max(w, h) * 0.10
        minx -= pad; maxx += pad; miny -= pad; maxy += pad

        # Keep a compact aspect ratio close to the supplied sample.
        fig_w, fig_h = figsize
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
        fig.patch.set_facecolor('white')
        ax.set_facecolor('white')
        ctx = RenderContext(insert_entity.doc if hasattr(insert_entity, 'doc') else None)
        # RenderContext on the original document is required for CAD color/layer
        # resolution. If it is unavailable, fall back to the layout document.
        try:
            doc = layout.doc
            ctx = RenderContext(doc)
        except Exception:
            pass
        backend = MatplotlibBackend(ax)
        Frontend(ctx, backend).draw_entities(ents)

        ax.set_aspect('equal', adjustable='box')
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        ax.axis('off')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.02, facecolor='white', dpi=dpi)
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        try:
            plt.close(fig)
        except Exception:
            pass
        return None


def _all_insert_info(data: bytes, exclude_legend=True, legend_radius_factor=0.04):
    """Return all INSERT references and only those located in a legend.

    The wider radius is used only to collect legend graphics; counting keeps the
    original conservative exclusion radius.
    """
    path = None
    try:
        import tempfile, ezdxf
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(data); path = Path(tmp.name)
        doc = ezdxf.readfile(path)
        all_infos=[]
        legend_infos=[]
        for layout in (doc.modelspace(),):
            inserts=[]; legend_points=[]; points=[]
            for entity in layout:
                et=entity.dxftype()
                if et=='INSERT':
                    xy=_xy(entity)
                    if xy: points.append(xy)
                    block_name=str(getattr(entity.dxf,'name','') or 'UNKNOWN').strip() or 'UNKNOWN'
                    info={'layout':layout.name,'block_name':block_name,'entity':entity,'xy':xy,'is_legend':False}
                    inserts.append(info); all_infos.append(info)
                elif et in ('TEXT','MTEXT'):
                    txt=re.sub(r'\s+',' ',_text_value(entity)).strip().upper()
                    if re.search(r'\b(LEGENDA|LEGEND|OZNACZENIA|OZNACZENIE)\b',txt):
                        xy=_xy(entity)
                        if xy: legend_points.append(xy)
            if not legend_points or not inserts:
                continue
            if points:
                xs=[p[0] for p in points]; ys=[p[1] for p in points]
                diag=max(((max(xs)-min(xs))**2+(max(ys)-min(ys))**2)**0.5,1.0)
            else:
                diag=1.0
            count_radius=diag*legend_radius_factor
            graphic_radius=diag*max(legend_radius_factor*3.0, 0.10)
            for info in inserts:
                xy=info['xy']
                if not xy:
                    continue
                d=min(((xy[0]-lx)**2+(xy[1]-ly)**2)**0.5 for lx,ly in legend_points)
                if d <= count_radius:
                    info['is_legend']=True
                    legend_infos.append(info)
                elif d <= graphic_radius:
                    # Legend graphics only. This never changes the counting logic.
                    info['is_legend_graphic']=True
                    legend_infos.append(info)
        return doc, all_infos, legend_infos
    except Exception:
        return None, [], []
    finally:
        try:
            if path:
                path.unlink(missing_ok=True)
        except Exception:
            pass


def collect_legend_graphics(uploaded_files, desired_blocks=None, exclude_legend=True):
    """Return block-name -> PNG screenshot only when the block occurs in the legend.

    There is deliberately no fallback to ordinary drawing occurrences. If a block
    is not found in the detected legend, its XLSX graphic cell stays empty.
    """
    wanted={str(x) for x in (desired_blocks or []) if str(x).strip()}
    graphics={}
    for f in uploaded_files or []:
        try:
            if Path(f.name).suffix.lower() != '.dxf':
                continue
            doc, all_infos, legend_infos = _all_insert_info(
                f.getvalue(), exclude_legend=exclude_legend
            )
            if not doc:
                continue
            for info in legend_infos:
                bn=str(info['block_name'])
                en=_element_name(info['entity'], bn)
                key=en or bn
                if wanted and key not in wanted:
                    continue
                if key in graphics:
                    continue
                layout=doc.layouts.get(info['layout'])
                img=render_legend_screenshot_png(layout, info['entity'], info['xy'])
                if img:
                    graphics[key]=img
        except Exception:
            continue
    return graphics


def render_selected_insert_png(data, block_name, exclude_legend=True, figsize=(4.8, 2.1), dpi=150, entity_handle=''):
    """Render a selected DXF INSERT like the 2.62.7 preview.
    Uses ezdxf's CAD renderer first, then falls back to the lightweight renderer.
    """
    path = None
    try:
        import tempfile, ezdxf
        from ezdxf import bbox
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(data); path = Path(tmp.name)
        doc = ezdxf.readfile(path)
        target = str(block_name or '').strip()
        if not target:
            return None
        candidates=[]
        for layout in (doc.modelspace(),):
            for ent in layout:
                if ent.dxftype() != 'INSERT':
                    continue
                try: name=str(ent.dxf.name or '').strip()
                except Exception: name=''
                handle = str(getattr(ent.dxf, 'handle', '') or '').strip()
                # Prefer the exact INSERT handle captured during analysis.
                # Fall back to block name for legacy result rows.
                if entity_handle and handle.casefold() != str(entity_handle).strip().casefold():
                    continue
                if not entity_handle and name != target and name.casefold() != target.casefold():
                    continue
                # Do not reject anonymous blocks just because they contain text;
                # legend exclusion is handled by the analysis itself.
                candidates.append((layout, ent))
        if not candidates:
            return None
        layout, ent = candidates[0]
        ents=list(ent.virtual_entities())
        if not ents:
            return None
        box=bbox.extents(ents, fast=True)
        if not box.has_data:
            return None
        minx,miny=float(box.extmin.x),float(box.extmin.y)
        maxx,maxy=float(box.extmax.x),float(box.extmax.y)
        w=max(maxx-minx,1e-6); h=max(maxy-miny,1e-6)
        pad=max(w,h)*0.35
        fig,ax=plt.subplots(figsize=figsize,dpi=dpi)
        fig.patch.set_facecolor('#05090d'); ax.set_facecolor('#05090d')
        rendered=False
        try:
            from ezdxf.addons.drawing import RenderContext, Frontend
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
            from ezdxf.addons.drawing.config import Configuration, ColorPolicy, BackgroundPolicy
            ctx=RenderContext(doc)
            # Force a high-contrast CAD preview: white geometry on the dark UI.
            cfg = Configuration.defaults().with_changes(
                color_policy=ColorPolicy.MONOCHROME_DARK_BG,
                background_policy=BackgroundPolicy.CUSTOM,
                custom_bg_color='#05090d'
            )
            Frontend(ctx, MatplotlibBackend(ax), config=cfg).draw_entities(ents)
            rendered=True
        except Exception:
            pass
        if not rendered:
            for sub in ents: _render_entity(ax, sub)
        ax.set_xlim(minx-pad,maxx+pad); ax.set_ylim(miny-pad,maxy+pad)
        ax.set_aspect('equal',adjustable='box'); ax.axis('off')
        buf=io.BytesIO(); fig.savefig(buf,format='png',bbox_inches='tight',facecolor='#05090d',edgecolor='none',pad_inches=0.03)
        plt.close(fig); return buf.getvalue()
    except Exception:
        return None
    finally:
        try:
            if path: path.unlink(missing_ok=True)
        except Exception: pass

