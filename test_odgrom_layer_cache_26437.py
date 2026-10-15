from pathlib import Path


def test_odgrom_layer_list_is_cached_before_dxf_parse():
    source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
    start = source.index("_og_cache_key = _file_content_cache_key(ogf)")
    end = source.index("suggested=[x for x in layers", start)
    block = source[start:end]
    assert "if _og_cache_key in _og_layer_cache" in block
    assert "layers = list(_og_layer_cache[_og_cache_key])" in block
    assert "ezdxf.readfile(opath)" in block
    assert "_og_layer_cache[_og_cache_key] = list(layers)" in block
    assert block.index("if _og_cache_key in _og_layer_cache") < block.index("ezdxf.readfile(opath)")


def test_odgrom_dxf_bytes_are_read_for_explicit_calculation_only():
    source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
    start = source.index("if active_module == 2:")
    end = source.index("# PDF workspace: this branch", start)
    block = source[start:end]
    assert "raw = ogf.getvalue()  # Read the DXF bytes only when calculation is explicitly requested." in block
    assert block.index("if c2.button('⚡ Oblicz odgrom/uziom'") < block.index("raw = ogf.getvalue()  # Read the DXF bytes only when calculation is explicitly requested.")


def test_scope_audit_does_not_hash_entire_upload_on_each_rerun():
    source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
    start = source.index("def _render_model_layout_scope_audit")
    end = source.index("st.set_page_config", start + 5)
    block = source[start:end]
    assert "content_key = _file_content_cache_key(file_obj)" in block
    assert "hashlib.sha1(raw)" not in block
