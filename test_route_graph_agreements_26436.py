import io
import pandas as pd
import ezdxf
from thanos.cable_list import measure_dxf_cable_routes, estimate_cable_paths_without_route_layers

class Upload:
    def __init__(self, name, data):
        self.name, self.data = name, data
    def getvalue(self):
        return self.data

def as_upload(doc, name='plan.dxf'):
    stream = io.StringIO(); doc.write(stream)
    return Upload(name, stream.getvalue().encode())

def test_electrical_and_telecom_routes_do_not_merge_at_shared_endpoint():
    doc = ezdxf.new('R2013'); doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    msp.add_line((0, 0), (1000, 0), dxfattribs={'layer': 'ELE_TRASY'})
    msp.add_line((1000, 0), (1000, 1000), dxfattribs={'layer': 'TELE_TRASY'})
    result = measure_dxf_cable_routes(as_upload(doc), route_layers=['ELE_TRASY', 'TELE_TRASY'])
    assert len(result) == 2
    assert set(result['Typ sieci']) == {'elektryczna', 'teletechniczna'}

def test_crossing_without_shared_endpoints_is_not_a_connection():
    doc = ezdxf.new('R2013'); doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    msp.add_line((-1000, 0), (1000, 0), dxfattribs={'layer': 'ELE_TRASY'})
    msp.add_line((0, -1000), (0, 1000), dxfattribs={'layer': 'ELE_TRASY'})
    result = measure_dxf_cable_routes(as_upload(doc), route_layers=['ELE_TRASY'])
    assert len(result) == 2

def test_fallback_estimates_orthogonal_ceiling_path_from_exact_labels():
    doc = ezdxf.new('R2013'); doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    msp.add_text('RG', dxfattribs={'insert': (0, 0)})
    msp.add_text('TP1', dxfattribs={'insert': (3000, 4000)})
    upload = as_upload(doc)
    cables = pd.DataFrame([{'Rozdzielnica źródłowa': 'RG', 'Odbiornik / rozdzielnica docelowa': 'TP1', 'Oznaczenie obwodu': 'WL-01'}])
    result = estimate_cable_paths_without_route_layers([upload], cables)
    assert len(result) == 1
    assert result.iloc[0]['Długość trasy [m]'] == 7.0
    assert result.iloc[0]['Status'] == 'Szacunek — niepotwierdzony'

def test_fallback_does_not_guess_when_endpoint_label_is_duplicated():
    doc = ezdxf.new('R2013'); doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    msp.add_text('RG', dxfattribs={'insert': (0, 0)})
    msp.add_text('RG', dxfattribs={'insert': (500, 500)})
    msp.add_text('TP1', dxfattribs={'insert': (3000, 4000)})
    cables = pd.DataFrame([{'Rozdzielnica źródłowa': 'RG', 'Odbiornik / rozdzielnica docelowa': 'TP1'}])
    assert estimate_cable_paths_without_route_layers([as_upload(doc)], cables).empty
