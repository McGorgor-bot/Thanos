import pandas as pd


def test_length_formula_with_room_height_and_reserve():
    route = 20.0
    outside = 3.0
    room = 3.2
    source_height = 2.0
    device_height = 0.3
    reserve_pct = 10.0
    base = route + outside + max(0, room-source_height) + max(0, room-device_height)
    assert round(base * (1 + reserve_pct/100), 2) == 29.81


def test_route_missing_does_not_create_estimated_length():
    route = pd.Series([None, 10.0])
    existing = pd.Series([None, None])
    result = (route + 0).where(route.notna(), existing)
    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == 10.0
