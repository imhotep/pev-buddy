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


# --- #26: search cost bounded -------------------------------------------------


def test_search_ignores_one_character_and_rejects_huge_queries(client):
    assert client.get("/api/search", params={"q": "e"}).json() == []
    assert client.get("/api/search", params={"q": "  t  "}).json() == []
    assert client.get("/api/search", params={"q": "x" * 101}).status_code == 422
    assert client.get("/api/search", params={"q": "Te"}).json()  # two is enough


def test_prefix_and_substring_matching():
    from pathlib import Path
    import orjson
    import tempfile
    from pev_buddy.pois import POIIndex

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "places.json"
        p.write_bytes(orjson.dumps([
            ["a", "Cafe Encore", "cafe", "488 Post St", -122.41, 37.79],
            ["b", "Encore Karaoke", "bar", "1 X St", -122.42, 37.79],
            ["c", "Encore", "bar", "2 X St", -122.43, 37.79],
        ]))
        idx = POIIndex(p)
        names = [r["name"] for r in idx.search("encore")]
        assert names[:2] == ["Encore", "Encore Karaoke"]  # exact, then prefix
        assert "Cafe Encore" in names  # substring, 3+ characters
        assert [r["name"] for r in idx.search("en")] == ["Encore", "Encore Karaoke"]  # 2 chars: prefixes only


# --- #37: results with no address ---------------------------------------------


def test_place_without_address_says_which_street_it_is_near(client):
    res = client.get("/api/search", params={"q": "Corner Kiosk"}).json()
    kiosk = next(r for r in res if r["text"] == "Corner Kiosk")
    assert kiosk["address"] == "Near 10 Side St"


# --- #42, #43: more misplaced places, and the audit that finds them -----------


def test_second_round_of_misplaced_places_is_not_offered(client):
    for q in ("SFO", "Lake Merritt"):
        names = [r["text"] for r in client.get("/api/search", params={"q": q}).json()]
        assert not {"SFO Int'l Airport", "Lake Merritt Oakland"} & set(names), (q, names)


def test_place_audit_lists_only_unreviewed_suspects(capsys):
    from pev_buddy import place_audit

    rows = [
        ["a", "Lake Tahoe Oakland", "lake", None, -122.43, 37.78],  # suspect
        ["b", "Oakland Art Association", "civic_organization", "500 Terry Francois Blvd", -122.39, 37.77],  # has an address
        ["c", "Oakland Army Base", "military_site", None, -122.30, 37.82],  # outside the city: flagged elsewhere
        ["d", "Lake Merritt Oakland", "lake", None, -122.43, 37.79],  # already on the reviewed list
        ["e", "Mission Dolores Park", "park", None, -122.43, 37.76],  # names nowhere outside
    ]
    assert [r[0] for r in place_audit.suspects(rows)] == ["a"]


def test_place_audit_cli(tmp_path, capsys):
    import orjson
    from pev_buddy import place_audit

    (tmp_path / "places.json").write_bytes(orjson.dumps([["a", "Vista Point Sausalito", "park", None, -122.43, 37.79]]))
    assert place_audit.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "1 place records to review" in out and "Vista Point Sausalito" in out


def test_every_reviewed_bad_place_says_why():
    from pev_buddy.place_fixes import BAD_PLACES

    names = [n for n, _c, _w in BAD_PLACES]
    assert len(names) == len(set(names))
    for name, _cat, why in BAD_PLACES:
        assert len(why) > 20, name


# --- v60 QA pass: #49, #50, #52, #54.6 -------------------------------------


@pytest.fixture()
def padded_geocoder(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from pev_buddy.geocode import Geocoder

    # Overture files some 3rd St addresses under "03RD ST" and some under "3RD ST".
    table = pa.table(
        {
            "street": ["03RD ST", "3RD ST", "01ST ST", "3RD AVE"],
            "number": ["360", "500", "100", "360"],
            "unit": [None, None, None, None],
            "postcode": ["94107", "94107", "94105", "94118"],
            "lon": [-122.39824, -122.3961, -122.39759, -122.4618],
            "lat": [37.78304, 37.7812, 37.78949, 37.7832],
        }
    )
    p = tmp_path / "addresses.parquet"
    pq.write_table(table, p)
    return Geocoder(p)


def test_zero_padded_numbered_streets_are_found_and_read_normally(padded_geocoder):
    g = padded_geocoder
    assert [r["text"] for r in g.search("360 3rd St")] == ["360 3RD ST"]
    assert [r["text"] for r in g.search("100 1st St")] == ["100 1ST ST"]
    # Both spellings are one street.
    assert {r["number"] for r in g.search("3rd St")} == {"360", "500"}
    # An ordinal is a street name, not a house number.
    assert "3RD AVE" in {r["street"] for r in g.search("3rd")}
    hit = g.reverse(-122.39824, 37.78304)
    assert hit["street"] == "3RD ST"


def test_map_tap_label_has_no_zero_padding(padded_geocoder):
    from pev_buddy.geocode import display_address

    hit = padded_geocoder.reverse(-122.39824, 37.78304)
    assert display_address(hit["number"], hit["street"]) == "360 3rd St"


def _moped_route(features, start, end):
    from conftest import make_road_graph
    from pev_buddy.routing import find_route

    g = make_road_graph(features)
    (slon, slat), (elon, elat) = m_to_lonlat(*start), m_to_lonlat(*end)
    return find_route(g, slon, slat, elon, elat, vehicle="moped")


def test_moped_bike_path_is_named_and_measured():
    from conftest import road_features

    # Without Side Street's east block, the Cycle Path is the only way to F.
    feats = [f for f in road_features() if f["id"] != "s-side2"]
    r = _moped_route(feats, (200, 0), (200, -100))
    assert r.bike_paths == [{"name": "Cycle Path", "near": None, "distance_m": pytest.approx(100, abs=1)}]


def test_an_unnamed_bike_path_is_placed_by_the_street_it_leaves():
    from conftest import road_features

    feats = [f for f in road_features() if f["id"] != "s-side2"]
    for f in feats:
        if f["id"] == "s-vert2":
            f["properties"]["name"] = None
    r = _moped_route(feats, (100, 0), (200, -100))
    assert [(p["name"], p["near"]) for p in r.bike_paths] == [(None, "Main Street")]


def test_a_street_signed_as_a_bike_route_is_not_a_bike_path():
    from conftest import road_features

    feats = road_features()
    for f in feats:
        if f["id"] == "s-main2":
            f["properties"]["bike_desig"] = 0xFFFF
    r = _moped_route(feats, (100, 0), (200, 0))
    assert r.bike_paths == []


def test_moped_warning_says_where_the_bike_path_is(synth_data_dir):
    import orjson

    roads = orjson.loads((synth_data_dir / "roads.geojson").read_bytes())
    roads["features"] = [f for f in roads["features"] if f["id"] != "s-side2"]
    (synth_data_dir / "roads.geojson").write_bytes(orjson.dumps(roads))
    for stale in ("graph.npz", "graph_meta.json"):
        (synth_data_dir / stale).unlink(missing_ok=True)
    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        body = c.post("/api/route", json={"start": _pt(200, 0), "end": _pt(200, -100), "vehicle": "moped"}).json()
        assert "route uses 330 ft of bike path on Cycle Path, which is usually reserved for bicycles" in body["warnings"]
        # Other vehicles aren't warned.
        body = c.post("/api/route", json={"start": _pt(200, 0), "end": _pt(200, -100), "vehicle": "scooter"}).json()
        assert not any("bike path" in w for w in body["warnings"])


def test_union_square_itself_leads_the_search(client):
    rows = client.get("/api/search", params={"q": "Union Square"}).json()
    assert rows[0]["kind"] == "place" and rows[0]["text"].startswith("Union Square")
    assert rows[0]["lat"] == pytest.approx(37.78799) and rows[0]["lon"] == pytest.approx(-122.40752)


def test_added_places_and_name_fixes():
    rows = [
        ["o-1", "Oakland-San Francico Bay Bridge", "bridge", None, -122.3874, 37.78937],
        ["o-2", "Union Square", "convenience_store", "548 Market St", -122.40173, 37.78916],
    ]
    fixed = place_fixes.fix_places(rows)
    names = [r[1] for r in fixed]
    assert "Oakland-San Francisco Bay Bridge" in names and "Oakland-San Francico Bay Bridge" not in names
    # The square comes before the shop named after it.
    assert names.index("Union Square") < [i for i, r in enumerate(fixed) if r[0] == "o-2"][0]
    assert rows[0][1] == "Oakland-San Francico Bay Bridge", "the input rows are left alone"


# --- v61 QA pass: #56, #58 --------------------------------------------------


def test_fort_mason_goes_to_fort_mason():
    rows = [
        ["o-1", "Fort Mason", "historic_site", None, -122.40992, 37.80599],  # North Beach
        ["o-2", "Fort Mason Chapel", "event_or_party_service", "Pope Rd", -122.42738, 37.8058],
    ]
    fixed = [r for r in place_fixes.fix_places(rows) if r[1] == "Fort Mason"]
    assert len(fixed) == 1 and fixed[0][0] == "pev-fort-mason"
    assert fixed[0][4] == pytest.approx(-122.4315, abs=0.002)


def test_place_audit_finds_a_record_far_from_its_namesakes():
    from pev_buddy import place_audit

    cluster = [
        ["c1", "Test Fort Center", "event_venue", "2 Marina Blvd", -122.4312, 37.8061],
        ["c2", "Test Fort Chapel", "church", "Pope Rd", -122.4274, 37.8058],
        ["c3", "Test Fort Garden", "park", "Pope Rd", -122.4280, 37.8059],
    ]
    chain = [[f"k{i}", f"Test Chain {i}", "cafe", f"{i} Main St", -122.40 - i * 0.02, 37.76 + i * 0.01] for i in range(4)]
    rows = cluster + chain + [
        ["far", "Test Fort", "historic_site", None, -122.4099, 37.8060],  # 1.8 km east of its cluster
        ["near", "Test Fort Pier", "pier", None, -122.4300, 37.8070],  # close by: fine
        ["chain", "Test Chain", "cafe", None, -122.30, 37.70],  # namesakes spread out: no cluster
    ]
    found = {r[0]: d for r, d in place_audit.far_from_namesakes(rows)}
    assert list(found) == ["far"] and 1500 < found["far"] < 2100


def test_one_result_per_address_however_many_units(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from pev_buddy.geocode import Geocoder

    table = pa.table(
        {
            "street": ["01ST ST", "01ST ST", "01ST ST", "MARKET ST"],
            "number": ["100", "100", "100", "1"],
            "unit": ["100", "2700", None, "PH 2"],
            "postcode": ["94105"] * 4,
            "lon": [-122.3976, -122.3976, -122.3976, -122.3946],
            "lat": [37.7895, 37.7895, 37.7895, 37.7942],
        }
    )
    pq.write_table(table, tmp_path / "a.parquet")
    g = Geocoder(tmp_path / "a.parquet")
    assert [r["text"] for r in g.search("100 1st St")] == ["100 1ST ST"]
    # A lone unit record keeps its unit.
    assert [r["text"] for r in g.search("1 Market St")] == ["1 MARKET ST (PH 2)"]
