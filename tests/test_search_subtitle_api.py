"""Issue #2: /api/search exposes address for UI subtitles."""

import pytest
from fastapi.testclient import TestClient

from pev_buddy.api import create_app


@pytest.fixture()
def client(synth_data_dir):
    app = create_app(data_dir=str(synth_data_dir))
    with TestClient(app) as c:
        yield c


def test_search_place_includes_address(client):
    r = client.get("/api/search", params={"q": "bakery"})
    place = next(i for i in r.json() if i["kind"] == "place")
    assert place["text"] == "Test Bakery"
    assert place["address"] == "12 Test Way"


def test_search_station_includes_address(client):
    r = client.get("/api/search", params={"q": "charger"})
    st = next(i for i in r.json() if i["kind"] == "station")
    assert st["address"] == "123 Test Way"
