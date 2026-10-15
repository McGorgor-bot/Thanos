import json
import pandas as pd
from thanos.cable_list import suggest_cable_route_matches


def test_shortest_path_matches_source_and_destination_labels():
    cables = pd.DataFrame([{'Rozdzielnica źródłowa':'RG','Odbiornik / rozdzielnica docelowa':'TP1','Oznaczenie obwodu':'LZ1'}])
    routes = pd.DataFrame([{'ID trasy':'T001','Długość trasy [m]':12.5,
        'Etykiety w pobliżu':'RG, TP1, K200H60/TELE',
        'Ścieżki między etykietami [JSON]':json.dumps({'RG||TP1':12.5})}])
    got = suggest_cable_route_matches(cables, routes)
    assert len(got) == 1
    assert got.iloc[0]['ID trasy'] == 'T001'
    assert got.iloc[0]['Długość trasy [m]'] == 12.5


def test_nearby_labels_without_path_graph_are_not_auto_assigned():
    cables = pd.DataFrame([{'Rozdzielnica źródłowa':'RG','Odbiornik / rozdzielnica docelowa':'TP1','Oznaczenie obwodu':'LZ1'}])
    routes = pd.DataFrame([{'ID trasy':'T001','Długość trasy [m]':12.5,'Etykiety w pobliżu':'RG, TP1, K200H60/TELE'}])
    assert suggest_cable_route_matches(cables, routes).empty


def test_generic_tray_labels_are_not_endpoints():
    cables = pd.DataFrame([{'Rozdzielnica źródłowa':'RG','Odbiornik / rozdzielnica docelowa':'TP1'}])
    routes = pd.DataFrame([{'ID trasy':'T001','Długość trasy [m]':12.5,
        'Ścieżki między etykietami [JSON]':json.dumps({'K200H60/TELE||K300H60/ELE':12.5})}])
    assert suggest_cable_route_matches(cables, routes).empty


def test_ambiguous_route_is_not_auto_assigned():
    cables = pd.DataFrame([{'Rozdzielnica źródłowa':'RG','Odbiornik / rozdzielnica docelowa':'TP1'}])
    routes = pd.DataFrame([
        {'ID trasy':'T001','Długość trasy [m]':12.5,'Ścieżki między etykietami [JSON]':json.dumps({'RG||TP1':12.5})},
        {'ID trasy':'T002','Długość trasy [m]':14.0,'Ścieżki między etykietami [JSON]':json.dumps({'RG||TP1':14.0})},
    ])
    assert suggest_cable_route_matches(cables, routes).empty
