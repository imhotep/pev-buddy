import numpy as np
import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from pev_buddy.geo import fast_distances
from pev_buddy.geocode import Geocoder, display_address


@pytest.fixture()
def geocoder(tmp_path):
    table = pa.table(
        {
            "street": ["MISSION ST", "MARKET ST", "MARKET ST", "VAN NESS AVE", "HAYES ST"],
            "number": ["1066", "101", "900", "2300", None],
            "unit": [None, None, "PH 2", None, None],
            "postcode": ["94103", "94102", "94102", "94109", "94102"],
            "lon": [-122.4148, -122.4099, -122.4058, -122.4211, -122.4196],
            "lat": [37.7599, 37.7801, 37.7845, 37.7928, 37.7860],
        }
    )
    p = tmp_path / "addresses.parquet"
    pq.write_table(table, p)
    return Geocoder(p)


def test_exact_street_number(geocoder):
    res = geocoder.search("1066 Mission St")
    assert len(res) >= 1
    assert res[0]["street"] == "MISSION ST"
    assert res[0]["number"] == "1066"
    assert res[0]["text"] == "1066 MISSION ST"
    assert abs(res[0]["lon"] - (-122.4148)) < 1e-9


def test_street_only(geocoder):
    res = geocoder.search("Market St")
    streets = {r["street"] for r in res}
    assert streets == {"MARKET ST"}


def test_fuzzy_street(geocoder):
    res = geocoder.search("Vanness ave")
    assert res and res[0]["street"] == "VAN NESS AVE"


def test_no_results(geocoder):
    assert geocoder.search("zzz nowhere") == []
    assert geocoder.search("") == []


def test_reverse_finds_nearest_address(geocoder):
    # ~30 m north of 1066 Mission St.
    hit = geocoder.reverse(-122.4148, 37.7599 + 30 / 111_320)
    assert hit["street"] == "MISSION ST" and hit["number"] == "1066"
    assert 25 < hit["distance_m"] < 35


def test_reverse_respects_max_distance(geocoder):
    # ~400 m from the nearest address: nothing is "near" by default.
    far = (-122.4148, 37.7599 - 400 / 111_320)
    assert geocoder.reverse(*far) is None
    assert geocoder.reverse(*far, max_m=500)["number"] == "1066"


def test_reverse_matches_brute_force(tmp_path):
    # The grid lookup must agree with a scan of every row, including query
    # points near cell edges and outside the data's extent.
    rng = np.random.default_rng(7)
    n = 2000
    lon = -122.45 + rng.random(n) * 0.04
    lat = 37.75 + rng.random(n) * 0.04
    table = pa.table(
        {
            "street": [f"STREET {i % 37}" for i in range(n)],
            "number": [str(i) for i in range(n)],
            "unit": [None] * n,
            "postcode": ["94110"] * n,
            "lon": lon,
            "lat": lat,
        }
    )
    p = tmp_path / "addresses.parquet"
    pq.write_table(table, p)
    g = Geocoder(p)
    pts = np.column_stack((lon, lat))
    for qlon, qlat in zip(-122.452 + rng.random(300) * 0.044, 37.748 + rng.random(300) * 0.044):
        d = fast_distances(qlon, qlat, pts)
        hit = g.reverse(qlon, qlat)
        if d.min() > 150:
            assert hit is None
        else:
            i = int(np.argmin(d))
            assert (hit["street"], hit["number"]) == (f"STREET {i % 37}", str(i))
            assert hit["distance_m"] == pytest.approx(d[i])


def test_display_address():
    assert display_address("123", "VALENCIA ST") == "123 Valencia St"
    assert display_address("400", "3RD ST") == "400 3rd St"
    assert display_address(None, "O'FARRELL ST") == "O'Farrell St"
