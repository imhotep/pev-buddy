"""Static wiring checks for the web UI (no browser required)."""

import json
import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "web"


def _text(name: str) -> str:
    return (WEB / name).read_text()


def test_index_html_has_required_ids():
    html = _text("index.html")
    for i in (
        "search",
        "search-results",
        "start-label",
        "end-label",
        "use-location",
        "use-location-end",
        "reset-route",
        "route-summary",
        "map",
        "steps-drawer",
        "steps-close",
        "steps-title",
        "steps",
        "show-steps",
        "vehicle",
        "vehicle-desc",
    ):
        assert f'id="{i}"' in html, f"missing #{i} in index.html"


def test_app_js_binds_every_control_in_html():
    js = _text("app.js")
    for i in (
        "reset-route",
        "show-steps",
        "steps-close",
        "use-location",
        "use-location-end",
        "vehicle",
        "vehicle-desc",
    ):
        assert f'getElementById("{i}")' in js, f"app.js never references #{i}"


def test_map_click_guarded_by_route_shown():
    # While a route is on the map, clicks must only pan — the guard has to be
    # the first thing the click handler does, before any point is set.
    js = _text("app.js")
    handler = js.split('map.on("click"', 1)[1]
    guard = handler.find("if (state.routeShown) return;")
    first_action = handler.find("queryRenderedFeatures")
    assert 0 <= guard < first_action


def test_reset_clears_route_state():
    js = _text("app.js")
    reset_body = js.split("function resetTrip()", 1)[1].split("}", 1)[0]
    for cleared in ("startRef", "endRef", "routeShown", "pev-route", "route-summary", "steps-drawer"):
        assert cleared in reset_body, f"resetTrip does not clear {cleared}"


def test_cache_bust_versions_match():
    html = _text("index.html")
    css_v = re.search(r"style\.css\?v=(\d+)", html).group(1)
    js_v = re.search(r"app\.js\?v=(\d+)", html).group(1)
    assert css_v == js_v, "style.css and app.js should share one cache-bust version"


def test_pwa_wiring():
    html = _text("index.html")
    assert 'rel="manifest"' in html, "index.html never links the web app manifest"
    manifest = json.loads((WEB / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    assert any(i["sizes"] == "192x192" for i in manifest["icons"])
    assert any(i["sizes"] == "512x512" for i in manifest["icons"])
    assert (WEB / "sw.js").exists(), "service worker missing"
    js = _text("app.js")
    assert "serviceWorker" in js, "app.js never registers the service worker"
    assert "GeolocateControl" in js, "mobile location pin missing"
    assert "wakeLock" in js, "screen wake lock missing"


def test_poi_layers_clustered_with_distinct_kind_colors():
    js = _text("app.js")
    # Chargers, BikeLink lockers, and bike racks share one clustered source:
    # count bubbles when zoomed out, colored pins when zoomed in.
    assert '"pev-pois"' in js and "cluster: true" in js
    assert "point_count_abbreviated" in js, "clusters must show a count label"
    colors = dict(re.findall(r'"(charger|locker|rack)",\s*"(#[0-9a-f]+)"', js))
    assert set(colors) == {"charger", "locker", "rack"}, "all three POI kinds must be styled"
    assert len(set(colors.values())) == 3, "each POI kind needs its own pin color"
    # Clicking a locker must be able to open its bubble.
    assert "showBikeLinkPopup" in js
