from pathlib import Path
from thanos.cablofil_geometry import analyze_cablofil_blocks


def test_cablofil_geometry_on_uploaded_plan():
    path = Path('/mnt/data/CP BB BHWW plan instalacji IE PW 05052026(1).dxf')
    if not path.exists():
        return
    report = analyze_cablofil_blocks(path.name, path.read_bytes())
    assert report['liczba_wstawień'] == 24
    assert report['liczba_osi_z_geometrii'] == 23
    assert report['jednostki_opis'] == 'mm'
    names = {row['Typ koryta'] for row in report['typy']}
    assert {'CF 54/200', 'CF 54/100', 'CF 54/50'} <= names
    assert all('X1 osi' in row and 'Status' in row for row in report['elementy'])


def test_missing_axis_is_not_given_fake_length():
    path = Path('/mnt/data/CP BB BHWW plan instalacji IE PW 05052026(1).dxf')
    if not path.exists():
        return
    report = analyze_cablofil_blocks(path.name, path.read_bytes())
    missing = [r for r in report['elementy'] if r['X1 osi'] is None]
    assert len(missing) == 1
    assert missing[0]['Długość osi bloku [jedn. CAD]'] == 0
    assert 'brak linii osiowej' in missing[0]['Status']
