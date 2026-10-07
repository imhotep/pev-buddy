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
        "route-actions",
        "start-ride",
        "ride-banner",
        "ride-controls",
        "ride-mute",
        "ride-steps",
        "ride-recenter",
        "end-ride",
        "trip-status",
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
        "start-ride",
        "end-ride",
        "ride-mute",
        "ride-steps",
        "ride-recenter",
        "show-steps",
        "steps-close",
        "use-location",
        "use-location-end",
        "vehicle",
        "vehicle-desc",
    ):
        assert f'getElementById("{i}")' in js, f"app.js never references #{i}"


def test_map_click_offers_start_and_destination():
    # Empty-map clicks open a "Start here / Go here" popup rather than
    # silently picking an endpoint, and keep working while a route is shown.
    js = _text("app.js")
    handler = js.split('map.on("click", (e)', 1)[1].split("\n});\n", 1)[0]
    assert "showPickPopup(e.lngLat)" in handler
    assert "state.routeShown" not in handler
    actions = js.split("function pickActions(", 1)[1].split("\n}\n", 1)[0]
    assert '"Start here"' in actions and '"Go here"' in actions
    # Endpoints are draggable and reroute on drop.
    assert js.count("draggable: true") == 2
    assert 'startMarker.on("dragend"' in js and 'endMarker.on("dragend"' in js
    assert "/api/reverse" in js


def test_reset_clears_route_state():
    js = _text("app.js")
    reset_body = js.split("function resetTrip()", 1)[1].split("}", 1)[0]
    for cleared in ("startRef", "endRef", "routeShown", "pev-route", "route-summary", "steps-drawer"):
        assert cleared in reset_body, f"resetTrip does not clear {cleared}"


def test_route_requests_are_abortable_and_fits_are_layout_aware():
    js = _text("app.js")
    route_body = js.split("async function computeRoute()", 1)[1].split("\n}\n", 1)[0]
    assert "AbortController" in route_body and "signal" in route_body
    assert "state.routing" not in js, "a new request must supersede, not be dropped"
    reset_body = js.split("function resetTrip()", 1)[1].split("\n}\n", 1)[0]
    assert "cancelRouting()" in reset_body
    # fitBounds never starts from the current view or a phone-overflowing pad.
    assert "map.getBounds()" not in js
    assert "left: 380" not in js
    assert "alert(" not in js, "use in-page feedback, not blocking alerts"


def test_cache_bust_versions_match():
    html = _text("index.html")
    css_v = re.search(r"style\.css\?v=(\d+)", html).group(1)
    js_v = re.search(r"app\.js\?v=(\d+)", html).group(1)
    assert css_v == js_v, "style.css and app.js should share one cache-bust version"


def test_page_pinch_zoom_is_disabled_but_map_is_exempt():
    html = _text("index.html")
    assert "user-scalable=no" in html, "viewport must opt out of page zoom"
    js = _text("app.js")
    assert '"gesturestart"' in js, "iOS Safari page-pinch guard missing"
    assert 'closest("#map")' in js, "map canvas must stay exempt from the pinch guard"
    # A document-level touchmove guard broke map pinch on iOS — don't re-add it.
    assert '"touchmove"' not in js, "document touchmove guards interfere with map pinch"
    assert "touch-action: manipulation" in _text("style.css")


def test_ride_mode_wiring():
    js = _text("app.js")
    css = _text("style.css")
    # "Start ride" is offered once a route is shown; End ride leaves it.
    nav_body = js.split("function syncNavUI()", 1)[1].split("\n}\n", 1)[0]
    assert "route-actions" in nav_body and "routeShown" in nav_body
    assert 'getElementById("start-ride").addEventListener("click", startRide)' in js
    assert 'getElementById("end-ride").addEventListener("click", endRide)' in js
    # Hands-free: GPS watch, voice, wake lock, mute remembered safely.
    start_body = js.split("function startRide()", 1)[1].split("\n}\n", 1)[0]
    for needed in ("startRideTracking()", "acquireWakeLock()", "speak(", "ensureOrientation()"):
        assert needed in start_body, f"startRide never calls {needed}"
    tracking = js.split("function startRideTracking()", 1)[1].split("\n}\n", 1)[0]
    assert "watchPosition" in tracking
    assert "speechSynthesis" in js and "pev-voice-muted" in js
    storage = js.split("function storageGet(", 1)[1].split("// ----", 1)[0]
    assert storage.count("try {") == 2, "localStorage access must be guarded"
    # Compass rotation is throttled, not an easeTo per sensor event.
    orient = js.split("function bindOrientation()", 1)[1].split("\n}\n", 1)[0]
    assert "easeTo" not in orient and "rotateToHeading()" in orient
    # The full-screen ride layout is a phone rule; MapLibre controls step
    # clear of the banner and control bar.
    mobile = css.split("@media (max-width: 760px)", 1)[1]
    assert "body.riding #sidebar" in mobile
    assert "--ride-banner-edge" in mobile and "--ride-controls-edge" in mobile


def test_poi_layers_hide_while_navigating():
    # While a route is up, the POI pins and count bubbles declutter the map.
    js = _text("app.js")
    nav_body = js.split("function syncNavUI()", 1)[1].split("function ", 1)[0]
    for layer in ("pev-pois", "pev-clusters", "pev-cluster-count"):
        assert layer in nav_body, f"syncNavUI does not toggle layer {layer}"
    assert '"visibility"' in nav_body and '"none"' in nav_body


def test_welcome_modal_with_persistent_opt_out():
    html = _text("index.html")
    for i in ("welcome-modal", "welcome-never", "welcome-close"):
        assert f'id="{i}"' in html, f"missing #{i} in index.html"
    assert 'role="dialog"' in html and "aria-modal" in html
    js = _text("app.js")
    # Shown on first visit, skipped once the user opts out.
    assert "pev-welcome-dismissed" in js
    assert "localStorage.getItem" in js and "localStorage.setItem" in js
    # The persisted choice is gated on the checkbox, not the dismiss itself.
    close_body = js.split("function closeWelcome()", 1)[1].split("}", 1)[0]
    assert "welcomeNever.checked" in close_body


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
    # Navigations must be network-first: index.html pins every asset to a
    # ?v=N, so stale HTML would strand the app on an old version forever.
    sw = _text("sw.js")
    assert '"navigate"' in sw, "service worker must special-case navigations"
    nav = sw.split('"navigate"', 1)[1][:400]
    assert "fetch(" in nav.split("caches.match", 1)[0], "navigations must hit the network before the cache"


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
