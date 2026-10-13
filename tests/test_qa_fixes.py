"""Fixes for the QA / code-review issues #24, #25, #31, #36 (API side)."""

import re

import pytest
from fastapi.testclient import TestClient

from conftest import NODES, m_to_lonlat
from pev_buddy import place_fixes
from pev_buddy.api import create_app

COORDS = re.compile(r"-?\d+\.\d{3,}")


@pytest.fixture()
def client(synth_data_dir):
    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        yield c


def _pt(dx, dy):
    lon, lat = m_to_lonlat(dx, dy)
    return {"lat": lat, "lon": lon}


# --- #24: known-misplaced places --------------------------------------------


def test_known_misplaced_places_are_not_offered(client):
    names = [r["text"] for r in client.get("/api/search", params={"q": "Ferry Building"}).json()]
    assert "Ferry Building, Embarcadero" not in names


def test_bad_place_matching_is_by_name_and_category():
    assert place_fixes.is_bad_place("Ferry Building, Embarcadero", "historic_site")
    assert place_fixes.is_bad_place("FERRY BUILDING, EMBARCADERO", "historic_site")
    assert not place_fixes.is_bad_place("Ferry Building", "b2b_service")  # the correct record
    assert not place_fixes.is_bad_place("Ferry Building, Embarcadero", "pier")
    assert place_fixes.is_bad_place("Oakland Coliseum", "anything")  # every category
    for name, _cat, why in place_fixes.BAD_PLACES:
        assert why, name  # each entry says why it's wrong


# --- #25 / #31: errors riders can read ---------------------------------------


def test_point_in_the_water_gets_a_plain_message(client):
    # Inside the city box but far from any street (the synthetic network is tiny).
    r = client.post("/api/route", json={"start": _pt(0, 0), "end": _pt(3000, 3000)})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "off_network"
    assert d["message"] == "The destination isn't near a street PEV Buddy can route on. Pick a point closer to a road."
    assert not COORDS.search(d["message"]) and "node" not in d["message"]


def test_route_errors_are_coded_and_free_of_internals(client):
    cases = [
        client.post("/api/route", json={"start": _pt(0, 0), "end": _pt(0, -100), "vehicle": "emoto"}),
        client.post("/api/route", json={"start": {"station_id": "nope"}, "end": _pt(0, 0)}),
        client.post("/api/walk", json={"start": _pt(0, 0), "end": _pt(3000, 3000)}),
    ]
    codes = []
    for r in cases:
        assert r.status_code in (400, 404), r.text
        d = r.json()["detail"]
        assert set(d) == {"code", "message"}, d
        assert not COORDS.search(d["message"]), d
        codes.append(d["code"])
    assert codes == ["vehicle_not_street_legal", "unknown_station", "off_network"]


# --- #36: outside the city ----------------------------------------------------


def test_search_labels_places_outside_the_city_and_ranks_them_last(client):
    res = client.get("/api/search", params={"q": "Test Bakery"}).json()
    by_name = {r["text"]: r for r in res}
    assert by_name["Test Bakery"]["in_service_area"] is True
    assert by_name["Test Bakery Oakland"]["in_service_area"] is False
    assert [r["text"] for r in res].index("Test Bakery") < [r["text"] for r in res].index("Test Bakery Oakland")


def test_route_to_outside_the_city_says_so(client):
    r = client.post("/api/route", json={"start": _pt(0, 0), "end": {"lat": 37.80, "lon": -122.30}})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "outside_area" and "outside San Francisco" in d["message"]
