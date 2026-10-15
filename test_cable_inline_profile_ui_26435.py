from pathlib import Path


def test_inline_profile_workflow_is_in_cable_list_module():
    source = Path(__file__).resolve().parent.joinpath('app.py').read_text(encoding='utf-8')
    cable_page = source.split("if st.session_state.get('ui_page') == 'cables':", 1)[1]
    assert 'cable_inline_profile_files' in cable_page
    assert '🔎 Przeanalizuj rysunki i zaproponuj profil' in cable_page
    assert 'Zatwierdź profil i zastosuj w pomiarze tras' in cable_page
    assert "st.session_state['thanos_project_profile'] = _inline_profile" in cable_page
    assert 'inspect_dxf(_pf.name, _praw)' in cable_page
    assert 'inspect_pdf(_pf.name, _praw)' in cable_page
    assert 'build_profile(' in cable_page
