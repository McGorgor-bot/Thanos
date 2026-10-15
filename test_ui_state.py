from thanos.ui_state import resolve_active_module


def test_switch_pdf_preview_to_cad_resets_to_analysis():
    # PDF index 3 is preview, but CAD index 3 is Excel → ATH.
    assert resolve_active_module('cad', 'pdf', 3, 4) == (0, 'cad')


def test_switch_cad_to_pdf_resets_to_analysis():
    assert resolve_active_module('pdf', 'cad', 3, 5) == (0, 'pdf')


def test_same_context_preserves_selected_module():
    assert resolve_active_module('cad', 'cad', 2, 4) == (2, 'cad')
    assert resolve_active_module('pdf', 'pdf', 3, 5) == (3, 'pdf')


def test_invalid_or_stale_index_resets():
    assert resolve_active_module('cad', 'cad', 4, 4) == (0, 'cad')
    assert resolve_active_module('pdf', 'pdf', None, 5) == (0, 'pdf')
    assert resolve_active_module('pdf', 'pdf', True, 5) == (0, 'pdf')
