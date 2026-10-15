import io
from thanos.project_profile import inspect_dxf, build_profile

def test_dxf_profile_suggests_named_route_layer_and_preserves_cable_marking():
    import ezdxf
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (1000, 0), dxfattribs={'layer': 'KK_S_300'})
    msp.add_text('N2XH-J 3x2,5 mm2 RK/1.1', dxfattribs={'layer': 'OPIS'})
    stream = io.StringIO()
    doc.write(stream)
    result = inspect_dxf('sample.dxf', stream.getvalue().encode())
    assert result['format'] == 'DXF'
    assert any(x['Warstwa'] == 'KK_S_300' for x in result['warstwy'])
    assert result['wzorce']['typy_kabli'] == ['N2XH-J']
    assert 'RK/1.1' in result['wzorce']['oznaczenia_obwodów']

def test_profile_keeps_user_confirmed_rules():
    profile = build_profile('Projekt A', [{'plik':'a.dxf','wzorce':{}}], ['KK_S_300'], ['URZ'], 'mm', 3.2, 12)
    assert profile['warstwy_tras_kablowych'] == ['KK_S_300']
    assert profile['parametry_pomiaru']['wysokość_pomieszczenia_m'] == 3.2
    assert profile['parametry_pomiaru']['zapas_kabla_proc'] == 12

def test_dxf_profile_accepts_utf8_bom_and_leading_whitespace():
    import ezdxf
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (1, 1), dxfattribs={'layer': 'KK_S_300'})
    stream = io.StringIO()
    doc.write(stream)
    payload = ('\ufeff  ' + stream.getvalue()).encode('utf-8')
    result = inspect_dxf('bom.dxf', payload)
    assert any(x['Warstwa'] == 'KK_S_300' for x in result['warstwy'])


def test_project_profile_separates_plan_and_schematic_layers():
    import ezdxf
    def payload(layer):
        doc = ezdxf.new()
        doc.modelspace().add_line((0, 0), (1000, 0), dxfattribs={'layer': layer})
        stream = io.StringIO(); doc.write(stream); return stream.getvalue().encode()
    plan = inspect_dxf('plan instalacji.dxf', payload('KORYTA_TRASA'))
    schematic = inspect_dxf('schemat instalacji.dxf', payload('KORYTA_TRASA'))
    assert plan['rola_rysunku'] == 'plan'
    assert next(x for x in plan['warstwy'] if x['Warstwa'] == 'KORYTA_TRASA')['Sugestia'] == 'Kandydat: trasa na planie'
    assert schematic['rola_rysunku'] == 'schemat'
    assert next(x for x in schematic['warstwy'] if x['Warstwa'] == 'KORYTA_TRASA')['Sugestia'] == 'Geometria schematu — nie mierzyć jako trasy'


def test_project_profile_excludes_annotation_layers_from_route_suggestions():
    import ezdxf
    doc = ezdxf.new(); msp = doc.modelspace()
    for i in range(12): msp.add_line((i, 0), (i, 100), dxfattribs={'layer': 'EL_koryta_opis'})
    stream = io.StringIO(); doc.write(stream)
    result = inspect_dxf('plan instalacji.dxf', stream.getvalue().encode())
    row = next(x for x in result['warstwy'] if x['Warstwa'] == 'EL_koryta_opis')
    assert row['Sugestia'] == 'Opis / ramka — nie mierzyć jako trasy'


def test_cablofil_route_block_layer_is_suggested_and_previewed():
    import ezdxf
    doc = ezdxf.new()
    block = doc.blocks.new(name='2dcbIf_korcf54y_b200_h054')
    block.add_line((0, 0), (1000, 0))
    doc.modelspace().add_blockref('2dcbIf_korcf54y_b200_h054', (100, 200),
                                  dxfattribs={'layer': '2DE_PRO_CbIfEZ'})
    stream = io.StringIO()
    doc.write(stream)
    result = inspect_dxf('plan instalacji.dxf', stream.getvalue().encode())
    row = next(x for x in result['warstwy'] if x['Warstwa'] == '2DE_PRO_CbIfEZ')
    assert row['Sugestia'].startswith('Kandydat: bloki systemu tras')
    assert result['geometria_podgladu']['2DE_PRO_CbIfEZ']

def test_pdf_profile_includes_vector_page_diagnostics():
    import fitz
    from thanos.project_profile import inspect_pdf
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.draw_line((50, 100), (500, 100), color=(1, 0, 0), width=2)
    page.insert_text((50, 40), 'Plan tras kablowych skala 1:100')
    payload = doc.tobytes(); doc.close()
    result = inspect_pdf('plan tras.pdf', payload)
    assert result['format'] == 'PDF'
    assert result['skala_rozpoznana'] == '1:100'
    assert result['diagnostyka_wektorowa'][0]['rysunki_wektorowe'] >= 1
    assert result['diagnostyka_wektorowa'][0]['obwiednia_wektorów_pkt'] is not None
