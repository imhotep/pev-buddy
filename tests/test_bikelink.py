"""BikeLink data: fetch-filter logic and the store."""

import orjson
import pytest

from conftest import m_to_lonlat
from pev_buddy import bikelink_fetch
from pev_buddy.bikelink import BikeLinkStore


def _loc(**kw):
    base = {
        "url_id": 1,
        "human_name": "Salesforce Transit Center",
        "street_address": "98 Natoma St",
        "city": "San Francisco",
        "longitude": -122.39541,
        "latitude": 37.79075,
        "location_friendly_type": "eLocker",
        "coming_soon": False,
        "online": True,
        "num_spaces": 92,
        "num_available_spaces": 58,
        "access_device_types": [
            {"key": "bikelink_chip_card", "name": "BikeLink Chip Card"},
            {"key": "clipper_card", "name": "Clipper Card"},
        ],
    }
    base.update(kw)
    return base


def test_to_features_filters_vendors_coming_soon_and_bbox():
    bbox = (-122.55, 37.65, -122.30, 37.87)
    locations = [
        _loc(url_id=1),
        _loc(url_id=2, location_friendly_type="Vendor", human_name="Card Vendor"),
        _loc(url_id=3, coming_soon=True, human_name="Future Hub"),
        _loc(url_id=4, longitude=-121.0, latitude=37.7, human_name="Out of SF"),
    ]
    feats = bikelink_fetch.to_features(locations, bbox)
    assert [f["id"] for f in feats] == ["bl-1"]
    p = feats[0]["properties"]
    assert p["name"] == "Salesforce Transit Center"
    assert p["facility_type"] == "eLocker"
    assert p["address"] == "98 Natoma St"
    assert p["num_spaces"] == 92
    # stale live state must not leak into the static slice
    assert "num_available_spaces" not in p
    assert "online" not in p
    assert p["access_devices"] == ["BikeLink Chip Card", "Clipper Card"]
    assert feats[0]["geometry"]["coordinates"] == [-122.39541, 37.79075]


def test_to_features_empty_when_nothing_matches():
    assert bikelink_fetch.to_features([_loc(url_id=2, location_friendly_type="Vendor")], (-122.55, 37.65, -122.30, 37.87)) == []


@pytest.fixture()
def bl_file(tmp_path):
    lon0, lat0 = m_to_lonlat(0, 0)
    lon1, lat1 = m_to_lonlat(200, -100)
    path = tmp_path / "bikelink.geojson"
    path.write_bytes(
        orjson.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "bl-1",
                        "properties": {
                            "name": "Test Locker",
                            "facility_type": "eLocker",
                            "address": "123 Test Way",
                            "city": "SF",
                            "num_spaces": 20,
                            "access_devices": ["BikeLink App"],
                        },
                        "geometry": {"type": "Point", "coordinates": [lon0, lat0]},
                    },
                    {
                        "type": "Feature",
                        "id": "bl-2",
                        "properties": {
                            "name": "Far Locker",
                            "facility_type": "Bike Hangar",
                            "address": None,
                            "city": "SF",
                            "num_spaces": None,
                            "access_devices": [],
                        },
                        "geometry": {"type": "Point", "coordinates": [lon1, lat1]},
                    },
                ],
            }
        )
    )
    return path


def test_store_loads_and_sorts_nearest(bl_file):
    store = BikeLinkStore(bl_file)
    assert len(store) == 2
    near = m_to_lonlat(2, 0)
    ranked = store.nearest(near[0], near[1], limit=2)
    assert [s["id"] for s in ranked] == ["bl-1", "bl-2"]
    assert ranked[0]["distance_m"] < 5
    assert "num_available_spaces" not in ranked[0]
    assert "online" not in store.all()[0]
