import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from pev_buddy.geocode import Geocoder


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
