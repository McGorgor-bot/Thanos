from pathlib import Path
import io
import sys
import types

ROOT = Path(__file__).resolve().parent


def _install_streamlit_stub():
    if 'streamlit' not in sys.modules:
        sys.modules['streamlit'] = types.SimpleNamespace(
            session_state={},
            cache_data=lambda **kwargs: (lambda fn: fn),
            cache_resource=lambda **kwargs: (lambda fn: fn),
        )


def test_odgrom_measurement_uses_cached_geometry_extractor():
    source = (ROOT / 'thanos' / 'cad.py').read_text(encoding='utf-8')
    assert '@st.cache_data(show_spinner=False, max_entries=8)' in source
    assert 'def _extract_odgrom_uziom_geometry(data: bytes):' in source
    start = source.index('def measure_odgrom_uziom(')
    end = source.index('\n\n\n@st.cache_data', start)
    body = source[start:end]
    assert '_extract_odgrom_uziom_geometry(bytes(data))' in body
    assert 'ezdxf.readfile' not in body


def test_odgrom_geometry_results_and_layer_filtering():
    _install_streamlit_stub()
    import ezdxf
    from thanos.cad import measure_odgrom_uziom, _extract_odgrom_uziom_geometry

    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    msp.add_line((0, 0), (3000, 4000), dxfattribs={'layer': 'ELE_odgromowy fi 8'})
    msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 1000)], dxfattribs={'layer': 'ELE_uziomy'})
    stream = io.StringIO()
    doc.write(stream)
    data = stream.getvalue().encode('utf-8')

    # In-memory DXF data are parsed into plain records and then reused for filters.
    rows, blocks = _extract_odgrom_uziom_geometry(data)
    assert len(rows) == 2 and blocks == []

    all_geom, all_blocks = measure_odgrom_uziom('sample.dxf', data, unit_scale=0.001)
    assert abs(float(all_geom['Długość [m]'].sum()) - 7.0) < 1e-8
    assert set(all_geom['Warstwa']) == {'ELE_odgromowy fi 8', 'ELE_uziomy'}
    assert all_blocks.empty

    one_geom, _ = measure_odgrom_uziom(
        'sample.dxf', data, unit_scale=0.001, selected_layers={'ELE_uziomy'})
    assert len(one_geom) == 1
    assert abs(float(one_geom['Długość [m]'].sum()) - 2.0) < 1e-8

    scaled_geom, _ = measure_odgrom_uziom('sample.dxf', data, unit_scale=0.01)
    assert abs(float(scaled_geom['Długość [m]'].sum()) - 70.0) < 1e-8
