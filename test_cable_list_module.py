import io
from pathlib import Path

import ezdxf
import fitz
from thanos.cable_list import CABLE_COLUMNS, extract_cable_candidates, export_cable_list_xlsx


class Upload:
    def __init__(self, name, data):
        self.name = name
        self._data = data

    def getvalue(self):
        return self._data


def test_pdf_cable_candidate_and_xlsx_export():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Zasilanie RG do TP1: N2XH-J 5x16, obwód WL-12')
    data = doc.tobytes()
    doc.close()
    result = extract_cable_candidates([Upload('schemat.pdf', data)])
    assert len(result) == 1
    assert result.iloc[0]['Typ kabla'].upper() == 'N2XH-J 5X16'
    assert result.iloc[0]['Oznaczenie obwodu'] == 'WL-12'
    exported = export_cable_list_xlsx(result)
    assert exported[:2] == b'PK'


def test_dxf_only_modelspace_is_parsed():
    doc = ezdxf.new('R2013')
    doc.modelspace().add_text('Zasilanie: YDY 3x1,5', dxfattribs={'layer': 'KABLE'})
    doc.layouts.new('Arkusz 1').add_text('Zasilanie: YKY 5x10', dxfattribs={'layer': 'KABLE'})
    stream = io.StringIO()
    doc.write(stream)
    data = stream.getvalue().encode('utf-8')
    result = extract_cable_candidates([Upload('schemat.dxf', data)])
    assert len(result) == 1
    assert result.iloc[0]['Typ kabla'].upper() == 'YDY 3X1,5'
    assert 'Model Space' in result.iloc[0]['Strona / warstwa']


def test_unrecognized_file_does_not_crash():
    result = extract_cable_candidates([Upload('not.txt', b'hello')])
    assert result.empty
    assert result.attrs['errors']


def test_dxf_recovery_uses_binary_stream(monkeypatch):
    """The ezdxf recovery API requires a binary stream, not StringIO."""
    import ezdxf as ezdxf_module
    from ezdxf import recover
    original_read = ezdxf_module.read
    doc = ezdxf.new('R2013')
    doc.modelspace().add_text('Zasilanie: YKY 5x10')
    stream = io.StringIO()
    doc.write(stream)
    raw = stream.getvalue().encode('utf-8')

    def fail_normal_read(_stream):
        raise ValueError('force recovery path')

    def recover_binary(binary_stream):
        assert isinstance(binary_stream, io.BytesIO)
        return original_read(io.StringIO(binary_stream.getvalue().decode('utf-8'))), None

    monkeypatch.setattr(ezdxf_module, 'read', fail_normal_read)
    monkeypatch.setattr(recover, 'read', recover_binary)
    result = extract_cable_candidates([Upload('recovery.dxf', raw)])
    assert len(result) == 1
    assert result.iloc[0]['Typ kabla'].upper() == 'YKY 5X10'


def test_full_multi_part_cable_designation_is_preserved():
    from thanos.cable_list import _records_from_text
    result = _records_from_text('4x N2HX 1x120 + N2XH 1x70', 'schemat.dxf', 'Model Space / KABLE')
    assert len(result) == 1
    assert result[0]['Typ kabla'] == '4x N2HX 1x120 + N2XH 1x70'
    assert result[0]['Liczba równoległych kabli'] == 4


def test_plus_separated_cable_terms_stay_in_one_record():
    from thanos.cable_list import _records_from_text
    result = _records_from_text('Zasilanie TP1: 4x N2HX 1x120 + N2XH 1x70', 'schemat.dxf', 'Model Space / KABLE')
    assert len(result) == 1
    assert result[0]['Typ kabla'] == '4x N2HX 1x120 + N2XH 1x70'
