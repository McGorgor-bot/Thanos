"""Regression tests for generic coordinate-based cable schedule extraction."""
from pathlib import Path
import fitz
from thanos.cable_list import _pdf_schedule_grid_records, extract_cable_candidates

PDF = Path('/mnt/data/PW-IE-04-008.4_Schemat rozdzielnicy TA2-6.pdf')


def test_ta2_schedule_grid_reads_distinct_circuits_and_cable_sections():
    assert PDF.exists(), 'Uploaded sample PDF is needed for this integration test'
    doc = fitz.open(PDF)
    try:
        rows = _pdf_schedule_grid_records(doc[0], PDF.name, 1)
    finally:
        doc.close()
    by_circuit = {r['Oznaczenie obwodu']: r for r in rows}
    assert len(rows) >= 20
    assert by_circuit['TA2']['Typ kabla'] == 'N2XH-J 5x6mm²'
    assert by_circuit['STR2']['Typ kabla'] == 'N2XH-J 5x2,5mm²'
    assert by_circuit['O1']['Typ kabla'] == 'N2XH-J 3x1,5mm²'
    assert by_circuit['G1']['Typ kabla'] == 'N2XH-J 3x2,5mm²'
    assert 'Rezerwa' in by_circuit['O11']['Odbiornik / rozdzielnica docelowa']
    assert by_circuit['O11']['Typ kabla'] == ''
    assert all(r['Długość [m]'] is None for r in rows)


def test_full_pdf_extraction_prefers_grid_records():
    class Upload:
        name = PDF.name
        def getvalue(self):
            return PDF.read_bytes()
    df = extract_cable_candidates([Upload()])
    assert len(df) >= 20
    assert 'O1' in set(df['Oznaczenie obwodu'])
    assert 'G5' in set(df['Oznaczenie obwodu'])
    o1 = df[df['Oznaczenie obwodu'] == 'O1'].iloc[0]
    assert o1['Typ kabla'] == 'N2XH-J 3x1,5mm²'
    assert 'pewność odczytu' in o1['Uwagi']
