"""Bike rack fetch: Socrata row -> GeoJSON feature conversion."""

from pev_buddy import racks_fetch

BBOX = (-122.55, 37.65, -122.30, 37.87)


def _row(**kw):
    base = {
        "objectid": "12183",
        "address": "Belden Street",
        "location": "Belden Place",
        "street": "BUSH",
        "placement": "SIDEWALK",
        "racks": "2",
        "spaces": "4",
        "install_yr": "2017",
        "shape": {"type": "Point", "coordinates": [-122.4037425, 37.79090921]},
    }
    base.update(kw)
    return base


def test_to_features_maps_fields_and_ids():
    feats = racks_fetch.to_features([_row()], BBOX)
    assert len(feats) == 1
    f = feats[0]
    assert f["id"] == "rack-12183"
    p = f["properties"]
    assert p["name"] == "Belden Street"
    assert p["landmark"] == "Belden Place"
    assert p["placement"] == "Sidewalk"
    assert p["racks"] == 2
    assert p["spaces"] == 4
    assert p["install_yr"] == 2017
    assert f["geometry"]["coordinates"] == [-122.40374, 37.79091]


def test_to_features_skips_missing_geometry_and_out_of_bbox():
    rows = [
        _row(objectid="1", shape=None),
        _row(objectid="2", shape={"type": "Point", "coordinates": [-121.0, 37.7]}),
    ]
    assert racks_fetch.to_features(rows, BBOX) == []


def test_to_features_name_fallback_and_bad_numbers():
    feats = racks_fetch.to_features(
        [_row(address="", street="", racks="x", spaces=None, install_yr="")], BBOX
    )
    p = feats[0]["properties"]
    assert p["name"] == "Bike rack"
    assert p["racks"] is None and p["spaces"] is None and p["install_yr"] is None
