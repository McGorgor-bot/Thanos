import json
import pandas as pd
from thanos.cable_list import suggest_cable_route_matches

def test_nan_identifiers_do_not_match_literal_nan_route_label():
    cables = pd.DataFrame([{"Rozdzielnica źródłowa":float("nan"),"Oznaczenie obwodu":float("nan"),"Odbiornik / rozdzielnica docelowa":"TP1"}])
    routes = pd.DataFrame([{"ID trasy":"T001","Długość trasy [m]":10.0,"Etykiety w pobliżu":"nan, Koryto K200","Ścieżki między etykietami [JSON]":json.dumps({"nan||TP1":10.0})}])
    assert suggest_cable_route_matches(cables, routes).empty

def test_unique_source_destination_shortest_path_matches():
    cables = pd.DataFrame([{"Rozdzielnica źródłowa":"RG","Odbiornik / rozdzielnica docelowa":"TP1"}])
    routes = pd.DataFrame([{"ID trasy":"T001","Długość trasy [m]":10.0,"Ścieżki między etykietami [JSON]":json.dumps({"RG||TP1":10.0})}])
    result = suggest_cable_route_matches(cables, routes)
    assert len(result) == 1
