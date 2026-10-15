import io
from pathlib import Path

from thanos.cable_list import import_cable_list_table, measure_cablofil_csv_routes


def test_import_existing_cable_list_xlsx():
    p = Path('/mnt/data/Lista_kablowa_2026-10-12_19-34-09.xlsx')
    if not p.exists():
        return
    df = import_cable_list_table(p.read_bytes(), p.name)
    assert len(df) == 16
    assert 'Typ kabla' in df.columns
    assert df['Długość [m]'].isna().all()
    assert df['Status weryfikacji'].eq('Do weryfikacji').all()


def test_measure_cablofil_csv_groups_connected_axes_and_skips_bad_coordinates():
    payload = (
        'ID,Warstwa wstawienia,Typ koryta (NAME),Blok,X wstawienia,Y wstawienia,Obrót [°],Długość osi bloku [jedn. CAD],X1 osi,Y1 osi,X2 osi,Y2 osi,Status\n'
        '1,L,CF 54/200,B1,0,0,0,1000,0,0,1000,0,ok\n'
        '2,L,CF 54/200,B2,1000,0,0,1000,1000,0,2000,0,ok\n'
        '3,L,CF 54/200,B3,0,0,0,1000,0,0,1000,0,duplicate\n'
        '4,L,CF 54/100,B4,0,10000,0,1000,nan,10000,11000,10000,bad\n'
    ).encode()
    class Upload:
        name = 'test.csv'
        def getvalue(self): return payload
    routes = measure_cablofil_csv_routes(Upload())
    assert len(routes) == 1
    assert routes.iloc[0]['Liczba odcinków'] == 2
    assert routes.iloc[0]['Długość trasy [m]'] == 2.0
