"""SFMTA motorcycle parking: black-cap meter spaces grouped per blockface."""

from pev_buddy import motopark_fetch

BBOX = (-122.6, 37.6, -122.3, 37.9)


def _row(bf, street, num, lon, lat, flag="M", onoff="ON"):
    return {
        "blockface_id": bf, "street_name": street, "street_num": num, "active_meter_flag": flag,
        "on_offstreet_type": onoff, "shape": {"type": "Point", "coordinates": [lon, lat]},
    }


def test_spaces_group_per_blockface_with_counts():
    rows = [
        _row("370011", "CAPP ST", "171", -122.41848, 37.76364),
        _row("370011", "CAPP ST", "173", -122.41850, 37.76368, flag="U"),
        _row("104022", "04TH AVE", "298", -122.46222, 37.78317, flag="T"),
        _row("999", "FUTURE ST", "1", -122.40, 37.77, flag="L"),  # not yet installed
        _row("888", "FAR AWAY RD", "1", -121.0, 37.0),  # outside the bbox
    ]
    feats = {f["id"]: f for f in motopark_fetch.to_features(rows, BBOX)}
    assert set(feats) == {"moto-370011-on", "moto-104022-on"}
    capp = feats["moto-370011-on"]["properties"]
    assert capp == {"name": "Capp St", "address": "171 Capp St", "spaces": 2, "metered": 1, "offstreet": False}
    ave = feats["moto-104022-on"]["properties"]
    assert ave["name"] == "4th Ave" and ave["metered"] == 0  # T: meter out of service, space free


def test_extract_writes_geojson(tmp_path, monkeypatch):
    monkeypatch.setattr(motopark_fetch, "fetch_spaces", lambda url: [_row("1", "MAIN ST", "5", -122.4, 37.78)])
    out = tmp_path / "motoparking.geojson"
    assert motopark_fetch.extract_motoparking(BBOX, out) == 1
    assert b'"Main St"' in out.read_bytes()
