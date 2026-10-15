import io
from thanos.cable_list import measure_dxf_cable_routes

class Upload:
    def __init__(self, name, data): self.name, self.data = name, data
    def getvalue(self): return self.data

def test_measure_route_polyline_in_metres():
    import ezdxf
    doc = ezdxf.new('R2013')
    doc.header['$INSUNITS'] = 4
    doc.modelspace().add_lwpolyline([(0,0),(3000,0),(3000,4000)], dxfattribs={'layer':'ELE_TRASY_ADMINISTRACJA'})
    stream = io.StringIO(); doc.write(stream)
    result = measure_dxf_cable_routes(Upload('routes.dxf', stream.getvalue().encode()))
    assert len(result) == 1
    assert abs(result.iloc[0]['Długość trasy [m]'] - 7.0) < 0.001

def test_unknown_units_are_not_guessed():
    import ezdxf
    doc = ezdxf.new('R2013'); doc.header['$INSUNITS'] = 0
    doc.modelspace().add_line((0,0),(1000,0), dxfattribs={'layer':'ELE_TRASY_ADMINISTRACJA'})
    stream = io.StringIO(); doc.write(stream)
    try:
        measure_dxf_cable_routes(Upload('routes.dxf', stream.getvalue().encode()))
    except ValueError as e:
        assert 'Nieznane jednostki' in str(e)
    else:
        raise AssertionError('Unknown units should not be converted')
