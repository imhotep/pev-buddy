import orjson
import pytest
from fastapi.testclient import TestClient

from conftest import m_to_lonlat, road_features
from pev_buddy.api import create_app


@pytest.fixture()
def client(synth_data_dir):
    app = create_app(data_dir=str(synth_data_dir))
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["nodes"] == 13
    assert body["edges"] > 0
    assert body["stations"] == 1
    assert body["default_vehicle"] == "scooter"
    assert set(body["vehicles"]) == {
        "scooter", "emb", "euc", "ebike_c1", "ebike_c2", "ebike_c3", "moped"
    }
    # the 40 mph arterial is off-limits to the boards but not to e-bikes
    assert body["vehicles"]["scooter"] < body["vehicles"]["ebike_c1"]


def test_vehicles_endpoint(client):
    r = client.get("/api/vehicles")
    assert r.status_code == 200
    body = r.json()
    assert body["default"] == "scooter"
    assert [v["id"] for v in body["vehicles"]] == [
        "scooter", "emb", "euc", "ebike_c1", "ebike_c2", "ebike_c3", "moped", "emoto"
    ]
    by_id = {v["id"]: v for v in body["vehicles"]}
    assert by_id["emoto"]["routable"] is False
    assert by_id["scooter"]["routable"] is True
    for v in body["vehicles"]:
        assert v["label"] and v["description"]


def test_stations_list(client):
    r = client.get("/api/stations")
    assert r.status_code == 200
    stations = r.json()
    assert len(stations) == 1
    assert stations[0]["name"] == "Test Charger"


def test_bikelink_list(client):
    r = client.get("/api/bikelink")
    assert r.status_code == 200
    lockers = r.json()
    assert len(lockers) == 2
    assert {l["id"] for l in lockers} == {"bl-1", "bl-2"}
    assert all("facility_type" in l for l in lockers)


def test_bikelink_nearest(client):
    a = m_to_lonlat(0, 0)
    r = client.get("/api/bikelink/nearest", params={"lon": a[0], "lat": a[1], "limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body[0]["id"] == "bl-1"
    assert body[0]["distance_m"] < 5


def test_bikelink_in_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["bikelink"] == 2


def test_stations_nearest(client):
    a = m_to_lonlat(0, 0)
    r = client.get("/api/stations/nearest", params={"lon": a[0], "lat": a[1], "limit": 3})
    assert r.status_code == 200
    body = r.json()
    assert body[0]["id"] == "st-1"
    assert body[0]["distance_m"] < 5


def test_geocode(client):
    r = client.get("/api/geocode", params={"q": "Main St"})
    assert r.status_code == 200
    assert r.json()[0]["street"] == "MAIN ST"


def test_route_by_query_and_coords(client):
    f = m_to_lonlat(200, -100)
    r = client.post(
        "/api/route",
        json={"start": {"query": "Main St"}, "end": {"lon": f[0], "lat": f[1]}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["path"]) >= 2
    assert 295 < body["distance_m"] < 302
    assert body["steps"][0]["maneuver"] == "depart"
    assert body["steps"][-1]["maneuver"] == "arrive"
    assert body["end_label"] == "map location"
    assert body["vehicle"] == "scooter"  # default vehicle type


def test_route_coordinate_label_in_arrival(client):
    f = m_to_lonlat(200, -100)
    r = client.post(
        "/api/route",
        json={"start": {"query": "Main St"}, "end": {"lon": f[0], "lat": f[1], "label": "Cafe Encore"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["end_label"] == "Cafe Encore"
    assert body["steps"][-1]["instruction"] == "Arrive at Cafe Encore."


def test_route_between_station_and_coords(client):
    f = m_to_lonlat(200, -100)
    r = client.post(
        "/api/route",
        json={"start": {"station_id": "st-1"}, "end": {"lon": f[0], "lat": f[1]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["start_label"] == "Test Charger"


def test_route_unreachable_snaps_to_usable_junction(client):
    # I only touches the excluded motorway, so a destination there snaps to
    # the nearest usable junction (H on the 40 mph arterial, 100 m away).
    i = m_to_lonlat(-100, 100)
    a = m_to_lonlat(0, 0)
    r = client.post(
        "/api/route",
        json={"start": {"lon": a[0], "lat": a[1]}, "end": {"lon": i[0], "lat": i[1]}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert any("snapped" in w for w in body["warnings"])


def test_route_vehicle_param_and_segment_stats(client):
    f = m_to_lonlat(200, -100)
    r = client.post(
        "/api/route",
        json={"start": {"query": "Main St"}, "end": {"lon": f[0], "lat": f[1]}, "vehicle": "ebike_c3"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vehicle"] == "ebike_c3"
    assert "bike_lane" in body["segment_stats"]
    assert body["segment_stats"]["bike_lane"]["distance_m"] > 0
    # the moped prices dedicated bike lanes as a discouraged option and takes
    # the residential streets instead
    r = client.post(
        "/api/route",
        json={"start": {"query": "Main St"}, "end": {"lon": f[0], "lat": f[1]}, "vehicle": "moped"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vehicle"] == "moped"
    assert "bike_lane" not in body["segment_stats"]


def test_route_unknown_vehicle_is_400(client):
    r = client.post(
        "/api/route",
        json={"start": {"query": "Main St"}, "end": {"lon": -122.4, "lat": 37.78}, "vehicle": "Q"},
    )
    assert r.status_code == 400
    assert "unknown vehicle type" in r.json()["detail"]


def test_route_emoto_is_blocked(client):
    r = client.post(
        "/api/route",
        json={"start": {"query": "Main St"}, "end": {"lon": -122.4, "lat": 37.78}, "vehicle": "emoto"},
    )
    assert r.status_code == 400
    assert "not street-legal" in r.json()["detail"]


def test_route_sidewalk_warning(client):
    # Y is only reachable over the sidewalk s-walk.
    y = m_to_lonlat(300, -100)
    a = m_to_lonlat(0, 0)
    r = client.post(
        "/api/route",
        json={"start": {"lon": a[0], "lat": a[1]}, "end": {"lon": y[0], "lat": y[1]}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert any("sidewalk" in w for w in body["warnings"])


def test_route_arterial_warning(client):
    # H sits on the 40 mph arterial; an e-bike route should flag it.
    h = m_to_lonlat(-100, 0)
    a = m_to_lonlat(0, 0)
    r = client.post(
        "/api/route",
        json={"start": {"lon": a[0], "lat": a[1]}, "end": {"lon": h[0], "lat": h[1]}, "vehicle": "ebike_c1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert any("arterial" in w for w in body["warnings"])
    assert body["segment_stats"]["arterial"]["max_speed_mph"] == 40


def test_route_unknown_station_is_404(client):
    r = client.post(
        "/api/route",
        json={"start": {"station_id": "nope"}, "end": {"lon": -122.4, "lat": 37.78}},
    )
    assert r.status_code == 404


def test_bad_point_ref_is_400(client):
    r = client.post("/api/route", json={"start": {}, "end": {"lon": -122.4, "lat": 37.78}})
    assert r.status_code == 400


def test_search_merges_places_and_stations(client):
    r = client.get("/api/search", params={"q": "test"})
    assert r.status_code == 200, r.text
    kinds = {i["kind"] for i in r.json()}
    assert "place" in kinds and "station" in kinds


def test_search_place_result_shape(client):
    r = client.get("/api/search", params={"q": "bakery"})
    items = r.json()
    place = next(i for i in items if i["kind"] == "place")
    assert place["text"] == "Test Bakery"
    assert place["category"] == "Restaurant"
    assert place["lon"] is not None and place["lat"] is not None


def test_search_station_result_has_id(client):
    r = client.get("/api/search", params={"q": "charger"})
    items = r.json()
    st = next(i for i in items if i["kind"] == "station")
    assert st["station_id"] == "st-1"


def test_search_includes_addresses(client):
    r = client.get("/api/search", params={"q": "main st"})
    items = r.json()
    assert any(i["kind"] == "address" and "MAIN ST" in i["text"] for i in items)


def test_search_empty_query_is_empty(client):
    assert client.get("/api/search", params={"q": "  "}).json() == []


def test_search_includes_bikelink(client):
    r = client.get("/api/search", params={"q": "test locker"})
    items = r.json()
    bl = next(i for i in items if i["kind"] == "bikelink")
    assert bl["text"] == "Test Locker"
    assert bl["category"] == "Bike Parking"
    assert bl["station_id"] == "bl-1"


def test_bikelink_geojson_data_endpoint(client):
    r = client.get("/data/bikelink.geojson")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/geo+json")
    assert orjson.loads(r.content)["type"] == "FeatureCollection"


def test_route_to_bikelink_by_id(client):
    f = m_to_lonlat(200, -100)
    r = client.post(
        "/api/route",
        json={"start": {"station_id": "bl-1"}, "end": {"lon": f[0], "lat": f[1]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["start_label"] == "Test Locker"
