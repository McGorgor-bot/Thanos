import io

import ezdxf
import pandas as pd

from thanos.cable_list import estimate_cable_paths_without_route_layers


class Upload:
    def __init__(self, name, data):
        self.name = name
        self._data = data

    def getvalue(self):
        return self._data


def upload(doc, name="plan.dxf"):
    stream = io.StringIO()
    doc.write(stream)
    return Upload(name, stream.getvalue().encode("utf-8"))


def cable_row(source="TP1", circuit="TP1/S13", target="odbiorniki obwodu"):
    return pd.DataFrame([{
        "Rozdzielnica źródłowa": source,
        "Oznaczenie obwodu": circuit,
        "Odbiornik / rozdzielnica docelowa": target,
    }])


def test_shared_circuit_labels_are_estimated_once_as_a_tree():
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_text("TP1", dxfattribs={"insert": (0, 0)})
    for x in (1000, 2000, 3000):
        msp.add_text("TP1/S13", dxfattribs={"insert": (x, 0)})

    result = estimate_cable_paths_without_route_layers([upload(doc)], cable_row())

    assert len(result) == 1
    row = result.iloc[0]
    assert row["Oznaczenie obwodu"] == "TP1/S13"
    assert row["Liczba odbiorników"] == 3
    # Source plus the three receivers form one shared 3 m straight tree,
    # rather than three independent source-to-receiver sums (6 m).
    assert row["Długość trasy [m]"] == 3.0
    assert row["Status"] == "Szacunek wspólnego przebiegu — niepotwierdzony"
    assert row["Długość orientacyjna z zapasem [m]"] > row["Długość trasy [m]"]


def test_separate_plan_clusters_are_not_joined_by_a_fake_long_segment():
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_text("TP1", dxfattribs={"insert": (0, 0)})
    for x in (1000, 2000, 100000, 101000):
        msp.add_text("TP1/S13", dxfattribs={"insert": (x, 0)})

    result = estimate_cable_paths_without_route_layers([upload(doc)], cable_row())

    assert len(result) == 1
    row = result.iloc[0]
    assert row["Liczba odbiorników"] == 4
    assert row["Liczba grup przestrzennych"] == 2
    # Local groups total 2 m + 1 m; no 98 m jump between separate views.
    assert row["Długość trasy [m]"] == 3.0
    assert "nie zostały dopisane" in row["Uwagi"]


def test_legacy_fallback_still_rejects_ambiguous_source_labels():
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_text("RG", dxfattribs={"insert": (0, 0)})
    msp.add_text("RG", dxfattribs={"insert": (500, 500)})
    msp.add_text("TP1", dxfattribs={"insert": (3000, 4000)})
    cables = pd.DataFrame([{
        "Rozdzielnica źródłowa": "RG",
        "Oznaczenie obwodu": "WL-01",
        "Odbiornik / rozdzielnica docelowa": "TP1",
    }])

    assert estimate_cable_paths_without_route_layers([upload(doc)], cables).empty


def test_unknown_dxf_units_do_not_produce_lengths():
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = 0
    msp = doc.modelspace()
    msp.add_text("TP1", dxfattribs={"insert": (0, 0)})
    msp.add_text("TP1/S13", dxfattribs={"insert": (1000, 0)})
    result = estimate_cable_paths_without_route_layers([upload(doc)], cable_row())
    assert result.empty
