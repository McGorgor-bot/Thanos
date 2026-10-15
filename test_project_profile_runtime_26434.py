import io, json, pathlib, sys
import pandas as pd
import ezdxf
from thanos.cable_list import measure_dxf_cable_routes

class Upload:
    def __init__(self, name, data): self.name=name; self._data=data
    def getvalue(self): return self._data

def make_dxf(units=0, layer="CUSTOM_ROUTE"):
    doc=ezdxf.new("R2010")
    doc.header["$INSUNITS"]=units
    doc.layers.new(layer)
    msp=doc.modelspace()
    msp.add_line((0,0),(1000,0),dxfattribs={"layer":layer})
    s=io.StringIO(); doc.write(s)
    return s.getvalue().encode()

def test_profile_route_layer_is_used():
    frame=measure_dxf_cable_routes(Upload("x.dxf",make_dxf(4)),route_layers=["CUSTOM_ROUTE"])
    assert len(frame)==1
    assert abs(float(frame.iloc[0]["Długość trasy [m]"])-1.0)<1e-6

def test_profile_unit_override_mm_when_dxf_units_unknown():
    frame=measure_dxf_cable_routes(Upload("x.dxf",make_dxf(0)),route_layers=["CUSTOM_ROUTE"],unit_override="mm")
    assert len(frame)==1
    assert abs(float(frame.iloc[0]["Długość trasy [m]"])-1.0)<1e-6

def test_auto_unit_unknown_still_fails_safely():
    try:
        measure_dxf_cable_routes(Upload("x.dxf",make_dxf(0)),route_layers=["CUSTOM_ROUTE"],unit_override="auto")
    except ValueError as exc:
        assert "Nieznane jednostki" in str(exc)
    else:
        raise AssertionError("Unknown units should not be guessed")
