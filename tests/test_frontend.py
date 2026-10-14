"""Browser tests for the web UI.

These run the real frontend in headless Chrome (system Chrome via Playwright's
``channel="chrome"``, so no browser download is needed) against a live uvicorn
server bound to a throwaway port. They are skipped automatically when
Playwright or Chrome is not installed.
"""

import re
import threading
import time

import httpx
import pytest

from conftest import m_to_lonlat, write_synth_bundle

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8771"


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    import uvicorn

    from pev_buddy.api import create_app

    # The repo's real data/ is gitignored (built by `pev-buddy sync` at deploy
    # time), so the browser tests run against the synthetic bundle instead.
    data = write_synth_bundle(tmp_path_factory.mktemp("synth") / "data")
    app = create_app(data_dir=str(data))

    config = uvicorn.Config(app, host="127.0.0.1", port=8771, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/api/vehicles", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        pytest.fail("uvicorn test server did not come up")
    yield BASE
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:
            pytest.skip(f"system Chrome not available: {e}")
        yield b
        b.close()


@pytest.fixture()
def open_app(browser, live_server):
    """Factory: open the app in a fresh page, optionally with a viewport,
    extra init scripts and page options (e.g. is_mobile). Every page opened is
    checked for uncaught JS errors."""
    opened = []

    def _open(width=1280, height=800, init_scripts=(), **page_options):
        pg = browser.new_page(viewport={"width": width, "height": height}, **page_options)
        # Tests exercise the app, not the first-visit intro — pre-dismiss it.
        pg.add_init_script("localStorage.setItem('pev-welcome-dismissed', '1')")
        for script in init_scripts:
            pg.add_init_script(script)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        opened.append((pg, errors))
        pg.goto(f"{live_server}/", wait_until="networkidle")
        pg.wait_for_function("() => typeof setStart === 'function'")
        # Map data loaded: the loading overlay removes itself once layers are up.
        pg.wait_for_selector("#map-loading", state="detached", timeout=20000)
        return pg

    yield _open
    for pg, errors in opened:
        pg.close()
        assert not errors, f"uncaught JS errors on page: {errors}"


@pytest.fixture()
def page(open_app):
    return open_app()


# Scripted GPS and captured speech for ride-mode tests: window.__emitFix(lon,
# lat, extra) feeds every watchPosition callback; spoken text lands in
# window.__spoken instead of the speakers.
GEO_AND_SPEECH_MOCK = """
(() => {
  const watchers = new Map();
  let nextId = 1;
  const geo = {
    getCurrentPosition(ok, fail) {
      if (window.__lastFix) setTimeout(() => ok(window.__lastFix), 0);
      else if (fail) setTimeout(() => fail({ code: 2, message: 'no fix yet' }), 0);
    },
    watchPosition(ok) { const id = nextId++; watchers.set(id, ok); return id; },
    clearWatch(id) { watchers.delete(id); },
  };
  Object.defineProperty(navigator, 'geolocation', { value: geo, configurable: true });
  window.__watchers = watchers;
  window.__emitFix = (lon, lat, extra = {}) => {
    const fix = {
      coords: { longitude: lon, latitude: lat, accuracy: 5, heading: null, speed: null, ...extra },
      timestamp: Date.now(),
    };
    window.__lastFix = fix;
    for (const cb of [...watchers.values()]) cb(fix);
  };
  window.__spoken = [];
  speechSynthesis.speak = (u) => window.__spoken.push(u.text);
  speechSynthesis.cancel = () => {};
})();
"""


@pytest.fixture()
def ride_page(open_app):
    return open_app(init_scripts=[GEO_AND_SPEECH_MOCK])


def fix_at(page, dx, dy, **coords):
    """Feed one GPS fix at a synthetic-network point (meters from ORIGIN)."""
    lon, lat = m_to_lonlat(dx, dy)
    extra = ", ".join(f"{k}: {v}" for k, v in coords.items())
    page.evaluate(f"() => __emitFix({lon}, {lat}, {{ {extra} }})")


def banner(page):
    return {
        "distance": page.text_content("#ride-distance"),
        "instruction": page.text_content("#ride-instruction"),
        "maneuver": page.get_attribute("#ride-arrow", "data-maneuver"),
        "state": page.get_attribute("#ride-banner", "data-state"),
    }


def route_via_ui(page):
    """Set a start/destination through the same path the UI uses, on the
    synthetic network: node A (Main Street) to node F (end of the cycle path)."""
    alon, alat = m_to_lonlat(0, 0)
    flon, flat = m_to_lonlat(200, -100)
    page.evaluate(
        f"""() => {{
          setStart({{ kind: 'coords', lat: {alat}, lon: {alon}, label: 'Test start' }});
          setEnd({{ kind: 'coords', lat: {flat}, lon: {flon}, label: 'Test end' }});
        }}"""
    )
    page.wait_for_function("() => document.querySelectorAll('#steps li').length > 0", timeout=15000)


def test_homepage_loads(page):
    assert "PEV Buddy" in page.title()
    assert page.is_visible("h1")
    assert page.is_visible("#map canvas")
    # The canvas carries a descriptive accessible name, not just "Map".
    label = page.get_attribute("#map canvas", "aria-label")
    assert label and "San Francisco" in label
    # POIs render through the clustered source: pins + count bubbles.
    layers = page.evaluate(
        "() => ['pev-pois', 'pev-clusters', 'pev-cluster-count'].map((id) => !!map.getLayer(id))"
    )
    assert all(layers)


def test_vehicle_select_populated_from_api(page):
    options = page.eval_on_selector_all("#vehicle option", "els => els.map(e => e.value)")
    assert len(options) >= 3  # scooter, ebike, bike, ... from /api/vehicles
    assert page.input_value("#vehicle") in options
    assert page.text_content("#vehicle-desc").strip()
    # Switching vehicles updates the description.
    other = next(o for o in options if o != page.input_value("#vehicle"))
    page.select_option("#vehicle", other)
    assert page.input_value("#vehicle") == other


def test_search_results_and_escape(page):
    page.fill("#search", "bakery")
    page.wait_for_selector("#search-results:not(.hidden)")
    assert page.eval_on_selector_all("#search-results .result", "els => els.length") >= 1
    assert "Test Bakery" in page.text_content("#search-results")
    assert page.get_attribute("#search", "aria-expanded") == "true"
    # Result count is announced to screen readers.
    status = page.text_content("#search-status")
    assert "result" in status or "No matches" in status
    # Escape closes the dropdown and returns focus to the input.
    page.press("#search", "Escape")
    assert page.eval_on_selector("#search-results", "el => el.classList.contains('hidden')")
    assert page.evaluate("() => document.activeElement.id") == "search"


def test_station_popup_and_navigate(page):
    page.evaluate(
        """() => showStationPopup({
          id: 'st-1', name: 'Test Charger', brand: 'EVgo', address: '123 Test Way',
          phone: null, website: null, lat: 37.78, lon: -122.41,
        })"""
    )
    page.wait_for_selector(".maplibregl-popup-content")
    assert "Test Charger" in page.text_content(".bubble-title")
    # The popup is dark, not the MapLibre default white (regression guard for
    # the .maplibregl-* class-name bug).
    bg = page.eval_on_selector(
        ".maplibregl-popup-content", "el => getComputedStyle(el).backgroundColor"
    )
    assert bg == "rgb(29, 43, 58)"  # var(--panel-2)
    # "Navigate here" sets the destination through the normal trip flow.
    page.click(".bubble-route")
    assert page.text_content("#end-label") == "Test Charger"


def test_console_debug_tools(page):
    # Idle until called: no HUD on a normal load.
    assert page.evaluate("() => typeof pev.help") == "function"
    assert not page.is_visible("#pev-debug-hud")
    assert page.evaluate("() => pev.hud()") == "HUD on"
    page.wait_for_timeout(400)
    assert "dragPan" in page.text_content("#pev-debug-hud")
    # Remembered across reloads, and switched off cleanly.
    page.reload()
    page.wait_for_selector("#map-loading", state="detached", timeout=20000)
    page.wait_for_selector("#pev-debug-hud")
    assert page.evaluate("() => pev.hud()") == "HUD off"
    assert not page.is_visible("#pev-debug-hud")

    route_via_ui(page)
    summary = page.evaluate("() => pev.route()")
    assert summary["miles"] > 0 and "segment_stats" in summary
    assert page.evaluate("() => pev.state().routeShown") is True


def test_debug_panel_buttons_and_title_taps(ride_page):
    page = ride_page
    assert not page.is_visible("#pev-debug-hud")
    # No console on a phone: five quick taps on the title open the panel.
    for _ in range(5):
        page.click("#sidebar header h1")
    page.wait_for_selector("#pev-debug-hud")
    route_via_ui(page)
    page.click("#pev-debug-hud button:text-is(\"Route\")")
    out = page.text_content("#pev-debug-hud .pev-dbg-out")
    assert out.startswith("route") and '"miles"' in out and "steps" in out
    page.click("#pev-debug-hud button:text-is(\"State\")")
    assert '"routeShown": true' in page.text_content("#pev-debug-hud .pev-dbg-out")
    # Drive from the panel: a fast simulated ride reaches the destination.
    page.fill("#pev-debug-hud input", "200")
    page.click("#pev-debug-hud button:text-is(\"▶ Drive\")")
    page.wait_for_function("() => ride.arrived", timeout=15000)
    # Minimize keeps only the bug button; close removes the panel.
    page.click("#pev-debug-hud .pev-dbg-min")
    assert not page.is_visible("#pev-debug-hud .pev-dbg-readout")
    page.click("#pev-debug-hud .pev-dbg-min")
    # Close sits in the top-right corner.
    panel, close = rect(page, "#pev-debug-hud"), rect(page, "#pev-debug-hud .pev-dbg-close")
    assert panel["right"] - close["right"] < 20 and close["top"] - panel["top"] < 20, (panel, close)
    page.click("#pev-debug-hud .pev-dbg-close")
    assert not page.is_visible("#pev-debug-hud")


def test_debug_panel_drags_stays_on_screen_and_remembers(page):
    page.evaluate("() => pev.hud(true)")
    grip = page.locator("#pev-debug-hud .pev-dbg-grip")
    g = grip.bounding_box()
    gx, gy = g["x"] + g["width"] / 2, g["y"] + g["height"] / 2
    page.mouse.move(gx, gy)
    page.mouse.down()
    page.mouse.move(gx + 300, gy + 200, steps=8)
    page.mouse.up()
    box = rect(page, "#pev-debug-hud")
    assert abs(box["left"] - (g["x"] - 4 + 300)) < 20 and box["top"] > 150, box
    # Flung far off screen: it stops at the edge.
    g = grip.bounding_box()
    page.mouse.move(g["x"] + 5, g["y"] + 5)
    page.mouse.down()
    page.mouse.move(5000, 5000, steps=4)
    page.mouse.up()
    box = rect(page, "#pev-debug-hud")
    assert box["right"] <= 1280 + 1 and box["top"] < 800, box
    # Remembered across a reload.
    page.reload()
    page.wait_for_selector("#pev-debug-hud")
    again = rect(page, "#pev-debug-hud")
    assert abs(again["left"] - box["left"]) < 2 and abs(again["top"] - box["top"]) < 2
    # Minimized it can still be moved: the grip stays next to the bug button.
    page.click("#pev-debug-hud .pev-dbg-min")
    assert page.is_visible("#pev-debug-hud .pev-dbg-grip")
    assert not page.is_visible("#pev-debug-hud .pev-dbg-readout")
    g = grip.bounding_box()
    page.mouse.move(g["x"] + 5, g["y"] + 5)
    page.mouse.down()
    page.mouse.move(60, 300, steps=6)
    page.mouse.up()
    small = rect(page, "#pev-debug-hud")
    assert small["left"] < 100 and 250 < small["top"] < 350, small
    page.evaluate("() => pev.hud(false)")


def test_console_drive_rides_the_route_to_arrival(ride_page):
    page = ride_page
    route_via_ui(page)
    page.evaluate("() => pev.drive(200)")  # ~90 m/s: the 300 m route in a few ticks
    page.wait_for_function("() => ride.arrived", timeout=15000)
    assert page.get_attribute("#ride-banner", "data-state") == "arrived"


def test_route_summary_shows_climb_only_when_there_is_one(page):
    # The synthetic bundle has no elevation data: no climb figure at all.
    route_via_ui(page)
    assert "↗" not in page.text_content("#route-summary")
    page.evaluate(
        """() => { const r = { ...state.route, climb_m: 84 }; renderRoute(r); }"""
    )
    assert "↗ 280 ft" in page.text_content("#route-summary")


def test_route_summary_steps_and_reset(page):
    route_via_ui(page)
    summary = page.text_content("#route-summary")
    assert "mi" in summary and "steps" in summary
    assert page.eval_on_selector_all("#steps li", "els => els.length") >= 3
    assert page.is_visible("#steps-drawer")
    page.click("#reset-route")
    assert page.eval_on_selector("#route-summary", "el => el.classList.contains('hidden')")
    assert page.text_content("#start-label").startswith("Not set")
    assert page.text_content("#end-label").startswith("Not set")


def test_steps_keyboard_and_drawer_focus(page):
    route_via_ui(page)
    # Steps are buttons: focusable, with a pressed state.
    first = page.eval_on_selector(
        "#steps li", "el => [el.getAttribute('role'), el.getAttribute('aria-pressed'), el.tabIndex]"
    )
    assert first == ["button", "false", 0]
    # Enter highlights the step on the map.
    page.focus("#steps li:nth-child(2)")
    page.keyboard.press("Enter")
    assert page.get_attribute("#steps li:nth-child(2)", "aria-pressed") == "true"
    # Closing the drawer returns focus to the "Show steps" toggle...
    page.click("#steps-close")
    assert page.evaluate("() => document.activeElement.id") == "show-steps"
    assert page.get_attribute("#show-steps", "aria-expanded") == "false"
    # ...and reopening moves focus into the drawer.
    page.click("#show-steps")
    assert page.evaluate("() => document.activeElement.id") == "steps-close"
    assert page.get_attribute("#show-steps", "aria-expanded") == "true"


def test_welcome_modal_first_visit_and_opt_out(browser, live_server):
    # A fresh profile (no pre-dismissed flag) gets the intro on first load.
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.goto(f"{live_server}/", wait_until="networkidle")
    assert pg.is_visible("#welcome-modal")
    assert "PEV Buddy" in pg.text_content("#welcome-title")
    # Focus starts on the dismiss button.
    assert pg.evaluate("() => document.activeElement.id") == "welcome-close"
    # Dismiss without the checkbox: modal closes, nothing persisted, so a
    # reload shows it again.
    pg.click("#welcome-close")
    assert not pg.is_visible("#welcome-modal")
    assert pg.evaluate("() => localStorage.getItem('pev-welcome-dismissed')") is None
    pg.reload(wait_until="networkidle")
    assert pg.is_visible("#welcome-modal")
    # With the checkbox: the choice persists across reloads.
    pg.check("#welcome-never")
    pg.click("#welcome-close")
    pg.reload(wait_until="networkidle")
    assert not pg.is_visible("#welcome-modal")
    pg.close()
    ctx.close()


def _load_map_recording_data_requests(browser, live_server, setup=None):
    pg = browser.new_page(viewport={"width": 1280, "height": 800})
    pg.add_init_script("localStorage.setItem('pev-welcome-dismissed', '1')")
    seen = []
    pg.on("response", lambda r: "/data/roads" in r.url and seen.append((r.url.split("/data/", 1)[1], r.status)))
    if setup:
        setup(pg)
    pg.goto(f"{live_server}/", wait_until="networkidle")
    pg.wait_for_selector("#map-loading", state="detached", timeout=20000)
    n = pg.evaluate("() => map.getSource('pev-roads')._data.features.length")
    pg.close()
    return seen, n


def test_map_prefers_display_roads_and_falls_back(browser, live_server):
    import orjson

    from conftest import road_features
    from pev_buddy.display_roads import build_display_roads

    # The synthetic bundle has no display file: the server 404s it and the
    # map falls back to the full extract, still drawing every road.
    seen, n = _load_map_recording_data_requests(browser, live_server)
    assert ("roads.display.geojson", 404) in seen
    assert ("roads.geojson", 200) in seen
    assert n == len(road_features())

    # With a display file available, the full extract is never requested.
    body = orjson.dumps(build_display_roads({"type": "FeatureCollection", "features": road_features()}))

    def serve_display(pg):
        pg.route(
            "**/data/roads.display.geojson",
            lambda route: route.fulfill(status=200, body=body, content_type="application/geo+json"),
        )

    seen, n = _load_map_recording_data_requests(browser, live_server, serve_display)
    assert all(name != "roads.geojson" for name, _ in seen), seen
    assert n == len(road_features())


def screen_xy(page, dx, dy, zoom=None):
    """Page coordinates of a synthetic-network point (meters from ORIGIN),
    optionally re-centering the map there first at the given zoom."""
    lon, lat = m_to_lonlat(dx, dy)
    if zoom is not None:
        page.evaluate(f"() => map.jumpTo({{ center: [{lon}, {lat}], zoom: {zoom} }})")
        page.wait_for_function("() => map.loaded() && !map.isMoving()")
    return page.evaluate(
        f"""() => {{
          const p = map.project([{lon}, {lat}]);
          const r = map.getCanvas().getBoundingClientRect();
          return [r.left + p.x, r.top + p.y];
        }}"""
    )


def test_empty_state_highlights_the_next_endpoint(page):
    assert page.eval_on_selector("#start-row", "el => el.classList.contains('needs-input')")
    assert not page.eval_on_selector("#end-row", "el => el.classList.contains('needs-input')")
    alon, alat = m_to_lonlat(0, 0)
    page.evaluate(f"() => setStart({{ kind: 'coords', lat: {alat}, lon: {alon}, label: 'Test start' }})")
    assert not page.eval_on_selector("#start-row", "el => el.classList.contains('needs-input')")
    assert page.eval_on_selector("#end-row", "el => el.classList.contains('needs-input')")


def test_map_click_popup_sets_start_with_address_label(page):
    # 40 m south of 5 MAIN ST (node A), on empty map.
    x, y = screen_xy(page, 0, -40, zoom=17)
    page.mouse.click(x, y)
    page.wait_for_selector(".maplibregl-popup .bubble.pick")
    page.wait_for_function(
        "() => document.querySelector('.bubble.pick .bubble-title').textContent === 'Near 5 Main St'"
    )
    page.click(".bubble-start")
    assert page.text_content("#start-label") == "Near 5 Main St"
    assert page.query_selector(".maplibregl-popup") is None
    # The first tap set the start — not, as it used to, the destination.
    assert page.text_content("#end-label").startswith("Not set")


def test_map_click_replaces_endpoint_while_route_is_shown(page):
    route_via_ui(page)
    x, y = screen_xy(page, 100, -100, zoom=17)  # node E, on Side Street
    page.mouse.click(x, y)
    page.wait_for_selector(".bubble.pick")
    with page.expect_response("**/api/route") as resp:
        page.click(".bubble-route")
    assert resp.value.ok
    page.wait_for_function("() => !document.getElementById('route-summary').textContent.includes('Finding')")
    assert "Test end" not in page.text_content("#route-summary")
    assert page.text_content("#end-label") != "Test end"


def test_dragging_start_pin_relabels_and_reroutes(page):
    route_via_ui(page)
    x0, y0 = screen_xy(page, 0, 0, zoom=17)
    x1, y1 = screen_xy(page, 0, -60)  # 40 m from 10 SIDE ST (node D)
    with page.expect_response("**/api/route"):
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x1, y1, steps=8)
        page.mouse.up()
    page.wait_for_function("() => document.getElementById('start-label').textContent === 'Near 10 Side St'")


def set_trip(page, end=(200, -100), end_label="Test end"):
    """Set start (node A) and a destination without waiting for the route."""
    alon, alat = m_to_lonlat(0, 0)
    elon, elat = m_to_lonlat(*end)
    page.evaluate(
        f"""() => {{
          setStart({{ kind: 'coords', lat: {alat}, lon: {alon}, label: 'Test start' }});
          setEnd({{ kind: 'coords', lat: {elat}, lon: {elon}, label: '{end_label}' }});
        }}"""
    )


def test_newer_route_request_wins_over_slow_older_one(page):
    # Hold the first /api/route response back so the second request finishes
    # first: the stale response must be dropped, not painted over the trip.
    page.evaluate(
        """() => {
          const realFetch = window.fetch;
          let calls = 0;
          window.fetch = (url, opts) => {
            if (String(url).includes('/api/route') && calls++ === 0) {
              return new Promise((r) => setTimeout(r, 800)).then(() => realFetch(url, opts));
            }
            return realFetch(url, opts);
          };
        }"""
    )
    set_trip(page, end_label="First end")
    set_trip(page, end=(200, 0), end_label="Second end")
    page.wait_for_function("() => document.querySelectorAll('#steps li').length > 0")
    page.wait_for_timeout(1200)  # well past the delayed first response
    summary = page.text_content("#route-summary")
    assert "Second end" in summary and "First end" not in summary


def test_reset_aborts_in_flight_route_and_clears_markers(page):
    set_trip(page)
    page.click("#reset-route")
    page.wait_for_timeout(800)  # the aborted response would have landed by now
    assert page.eval_on_selector("#route-summary", "el => el.classList.contains('hidden')")
    assert page.evaluate("() => map.querySourceFeatures('pev-route').length") == 0
    assert page.evaluate("() => document.querySelectorAll('.maplibregl-marker').length") == 0
    assert page.text_content("#start-label").startswith("Not set")
    assert page.text_content("#end-label").startswith("Not set")


def test_route_fit_zooms_in_and_padding_fits_a_phone(page):
    zoom_before = page.evaluate("() => map.getZoom()")
    route_via_ui(page)
    # Bounds come from the route alone, so a short route zooms right in.
    page.wait_for_function(f"() => !map.isMoving() && map.getZoom() > {zoom_before + 1}")
    # At phone width the padding still leaves map to fit into (the old
    # hard-coded left: 380 alone was wider than the screen).
    page.set_viewport_size({"width": 390, "height": 844})
    pad = page.evaluate("() => mapPadding()")
    assert pad["left"] + pad["right"] < 390 - 40
    assert pad["top"] + pad["bottom"] < 844 - 40


def test_search_result_subtitle_and_clear_after_pick(page):
    page.fill("#search", "bakery")
    page.wait_for_selector("#search-results:not(.hidden)")
    assert page.text_content("#search-results .result small") == "Restaurant · 12 Test Way"
    page.click("#search-results .result button:has-text('Start')")
    assert page.text_content("#start-label") == "Test Bakery"
    assert page.input_value("#search") == ""
    assert page.eval_on_selector("#search-results", "el => el.classList.contains('hidden')")


def test_ride_logic_pure_functions(page):
    # An L-shaped route: 200 m east, then 100 m south (the A -> C -> F route).
    path = [list(m_to_lonlat(0, 0)), list(m_to_lonlat(200, 0)), list(m_to_lonlat(200, -100))]
    r = page.evaluate(
        f"""() => {{
          const line = buildRouteLine({path});
          const snap = (p, from) => snapToRoute(line, p, from);
          return {{
            length: line.length,
            onLine: snap({list(m_to_lonlat(50, 0))}),
            offLine: snap({list(m_to_lonlat(120, 30))}),
            pastCorner: snap({list(m_to_lonlat(205, -40))}),
            // Equidistant from both legs: the hint keeps it on the later leg.
            hinted: snap({list(m_to_lonlat(150, -50))}, 210),
            positions: maneuverPositions(line, [
              {{ maneuver: 'depart', turn_point: null }},
              {{ maneuver: 'right', turn_point: {path[1]} }},
              {{ maneuver: 'arrive', turn_point: {path[2]} }},
            ]),
            next: [
              nextManeuver([0, 200, 300], 100, 1),
              nextManeuver([0, 200, 300], 195, 1),
              nextManeuver([0, 200, 300], 210, 1),
              nextManeuver([0, 200, 300], 150, 2), // jitter backwards: stays on 2
            ],
            off: [offRouteCount(0, 60, 5), offRouteCount(2, 60, 5), offRouteCount(2, 10, 5), offRouteCount(2, 60, 200), offRouteCount(2, 300, 200)],
            reroute: [shouldReroute(3, null, 0), shouldReroute(1, 0, 4000), shouldReroute(1, 0, 5000), shouldReroute(0, 0, 9000)],
            prompts: [promptLevel(400, 0), promptLevel(140, 0), promptLevel(140, 1), promptLevel(20, 1), promptLevel(20, 0), promptLevel(20, 2)],
            heading: [
              pickHeading({{ speed: 5, heading: 90 }}, 10),
              pickHeading({{ speed: 0.5, heading: 90 }}, 10),
              pickHeading({{ speed: 5, heading: NaN }}, null),
            ],
            rotate: [shouldRotate(0, 3, 1000), shouldRotate(0, 30, 100), shouldRotate(350, 20, 1000)],
            // A fast 200 m street, then a slow 100 m sidewalk, then arrival.
            toGo: [0, 100, 250, 300].map((along) => secondsToGo([
              {{ distance_m: 200, duration_s: 40 }},
              {{ distance_m: 100, duration_s: 60 }},
              {{ distance_m: 0, duration_s: 0 }},
            ], along)),
          }};
        }}"""
    )
    assert abs(r["length"] - 300) < 1
    assert r["onLine"]["distance"] < 0.5 and abs(r["onLine"]["along"] - 50) < 0.5
    assert abs(r["onLine"]["bearing"] - 90) < 1  # heading east
    assert abs(r["offLine"]["distance"] - 30) < 0.5 and abs(r["offLine"]["along"] - 120) < 0.5
    assert abs(r["pastCorner"]["along"] - 240) < 0.5 and abs(r["pastCorner"]["bearing"] - 180) < 1
    assert r["hinted"]["along"] > 200
    assert [round(p) for p in r["positions"]] == [0, 200, 300]
    assert [(n["index"], round(n["distance"])) for n in r["next"]] == [(1, 100), (1, 5), (2, 90), (2, 150)]
    assert r["off"] == [1, 3, 0, 2, 3]  # a vague fix counts only when off route even at its worst
    assert r["reroute"] == [True, False, True, False]
    assert r["prompts"] == [0, 1, 0, 2, 2, 0]
    assert r["heading"] == [90, 10, None]
    assert r["rotate"] == [False, False, True]
    # Time left follows each step's own pace, not the route-wide average.
    assert r["toGo"] == [100, 80, 30, 0]


def test_ride_mode_auto_advances_along_route(ride_page):
    page = ride_page
    route_via_ui(page)
    page.click("#start-ride")
    assert page.evaluate("() => document.body.classList.contains('riding')")
    assert page.is_visible("#ride-banner") and page.is_visible("#end-ride")
    assert page.evaluate("() => __spoken") == ["Head east on Main Street."]

    # At the start, heading east at speed: next is the right turn 200 m on,
    # and the map turns heading-up from the GPS course.
    fix_at(page, 0, 0, speed=5, heading=90)
    b = banner(page)
    assert b["instruction"] == "Turn right onto Cycle Path."
    assert b["maneuver"] == "right" and b["distance"] == "0.1 mi"
    page.wait_for_function("() => Math.abs(map.getBearing() - 90) < 1")

    # Inside 500 ft: the heads-up prompt and a live countdown in feet.
    fix_at(page, 60, 3)  # a few meters off the line still snaps onto it
    assert banner(page)["distance"] == "460 ft"
    assert page.evaluate("() => __spoken.at(-1)") == "In 450 feet, turn right onto Cycle Path."

    # Inside 100 ft: the turn-now prompt.
    fix_at(page, 185, 0)
    assert page.evaluate("() => __spoken.at(-1)") == "Turn right onto Cycle Path."

    # Past the corner: the banner advances to the arrival, and the overview
    # list marks it as the current step.
    fix_at(page, 200, -30)
    b = banner(page)
    assert b["instruction"] == "Arrive at Test end." and b["maneuver"] == "arrive"
    assert b["distance"] == "230 ft"
    assert page.get_attribute("#steps li:nth-child(3)", "aria-current") == "step"

    # Arrival: announced, GPS released, and a clear way out.
    fix_at(page, 200, -90)
    b = banner(page)
    assert b["state"] == "arrived" and "arrived at Test end" in b["instruction"]
    assert page.evaluate("() => __spoken.at(-1)") == "You have arrived at Test end."
    assert page.evaluate("() => __watchers.size") == 0
    page.click("#end-ride")
    assert not page.evaluate("() => document.body.classList.contains('riding')")
    assert not page.is_visible("#ride-banner")
    page.wait_for_function("() => Math.abs(map.getBearing()) < 1")


def test_ride_mode_reroutes_after_consecutive_off_route_fixes(ride_page):
    page = ride_page
    route_via_ui(page)
    page.click("#start-ride")
    fix_at(page, 0, 0)
    route_requests = []
    page.on("request", lambda r: route_requests.append(r) if "/api/route" in r.url else None)

    # Vague fixes (200 m accuracy) never trigger a reroute; neither do two
    # good off-route fixes in a row.
    for _ in range(3):
        fix_at(page, 100, 100, accuracy=200)
    fix_at(page, 100, 100)
    fix_at(page, 100, 100)
    page.wait_for_timeout(300)
    assert route_requests == []

    # The third consecutive one does — from the rider's current position.
    with page.expect_response("**/api/route") as resp:
        fix_at(page, 100, 100)
    assert resp.value.ok
    start = resp.value.request.post_data_json["start"]
    lon, lat = m_to_lonlat(100, 100)
    assert abs(start["lon"] - lon) < 1e-9 and abs(start["lat"] - lat) < 1e-9
    assert "Rerouting." in page.evaluate("() => __spoken")
    # Guidance continues on the new route.
    page.wait_for_function("() => document.getElementById('ride-status').classList.contains('hidden')")
    assert page.evaluate("() => document.body.classList.contains('riding')")
    assert page.text_content("#start-label") == "My location"


def test_ride_mode_reroutes_when_off_route_without_new_fixes(ride_page):
    # A stopped rider (or DevTools' location override) sends one fix and then
    # nothing: staying off route for OFF_ROUTE_MS still reroutes.
    page = ride_page
    route_via_ui(page)
    page.click("#start-ride")
    fix_at(page, 0, 0)
    with page.expect_response("**/api/route", timeout=8000) as resp:
        fix_at(page, 100, 100)
    start = resp.value.request.post_data_json["start"]
    lon, lat = m_to_lonlat(100, 100)
    assert abs(start["lon"] - lon) < 1e-9 and abs(start["lat"] - lat) < 1e-9


def test_ride_mode_back_on_route_cancels_pending_reroute(ride_page):
    page = ride_page
    route_via_ui(page)
    page.click("#start-ride")
    fix_at(page, 0, 0)
    route_requests = []
    page.on("request", lambda r: route_requests.append(r) if "/api/route" in r.url else None)
    fix_at(page, 100, 100)
    fix_at(page, 50, 0)  # back on the line before the timer runs out
    page.wait_for_timeout(5500)
    assert route_requests == []


def test_ride_voice_mute_is_remembered(ride_page):
    page = ride_page
    route_via_ui(page)
    page.click("#start-ride")
    assert page.get_attribute("#ride-mute", "aria-pressed") == "true"
    page.click("#ride-mute")
    assert page.get_attribute("#ride-mute", "aria-pressed") == "false"
    assert page.evaluate("() => localStorage.getItem('pev-voice-muted')") == "1"
    spoken = page.evaluate("() => __spoken.length")
    fix_at(page, 185, 0)  # would say "Turn right onto Cycle Path."
    assert page.evaluate("() => __spoken.length") == spoken
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#map-loading", state="detached", timeout=20000)
    route_via_ui(page)
    page.click("#start-ride")
    assert page.get_attribute("#ride-mute", "aria-pressed") == "false"
    assert page.evaluate("() => __spoken") == []


def rect(page, selector):
    return page.eval_on_selector(selector, "el => el.getBoundingClientRect().toJSON()")


def test_phone_layout_keeps_map_visible_and_targets_tappable(open_app):
    page = open_app(390, 844, init_scripts=[GEO_AND_SPEECH_MOCK])
    route_via_ui(page)
    # With a route up, the sheet collapses to summary + actions: most of the
    # screen is map, and the steps drawer stays closed.
    assert page.evaluate("() => document.body.classList.contains('sheet-collapsed')")
    assert rect(page, "#sidebar")["height"] < 844 * 0.35
    assert not page.is_visible("#steps-drawer")
    for sel in ("#start-ride", "#show-steps", "#sheet-toggle"):
        assert page.is_visible(sel)
        assert rect(page, sel)["height"] >= 44, f"{sel} is under 44px"
    # MapLibre's attribution sits above the sheet, not under it.
    assert rect(page, ".maplibregl-ctrl-bottom-right")["bottom"] <= rect(page, "#sidebar")["top"] + 1

    # "Edit trip" brings the full trip panel back, with 44px controls.
    page.click("#sheet-toggle")
    assert page.is_visible("#reset-route") and page.is_visible("#search")
    for sel in ("#reset-route", "#use-location", "#search", "#vehicle"):
        assert rect(page, sel)["height"] >= 44, f"{sel} is under 44px"
    page.click("#sheet-toggle")

    # Ride mode: full-screen map; banner and controls clear of MapLibre's.
    page.click("#start-ride")
    page.wait_for_timeout(100)  # let ResizeObserver publish the overlay edges
    assert not page.is_visible("#sidebar")
    banner_box = rect(page, "#ride-banner")
    controls_box = rect(page, "#ride-controls")
    assert rect(page, ".maplibregl-ctrl-top-right")["top"] >= banner_box["bottom"]
    assert rect(page, ".maplibregl-ctrl-bottom-right")["bottom"] <= controls_box["top"] + 1
    for sel in ("#ride-mute", "#ride-steps", "#end-ride"):
        assert rect(page, sel)["height"] >= 44
    # The steps overview opens between the two without covering either.
    page.click("#ride-steps")
    drawer = rect(page, "#steps-drawer")
    assert drawer["top"] >= banner_box["bottom"] and drawer["bottom"] <= controls_box["top"]


def framed_route(page):
    """Once the camera settles: the endpoint pins' boxes and the route's
    projected points, in viewport pixels."""
    page.wait_for_timeout(100)  # let a refit triggered by a resize start
    page.wait_for_function("() => !map.isMoving()")
    return page.evaluate(
        """() => ({
          pins: [startMarker, endMarker].map((m) => m.getElement().getBoundingClientRect().toJSON()),
          path: state.route.path.map((c) => {
            const p = map.project(c);
            const m = map.getContainer().getBoundingClientRect();
            return { x: p.x + m.left, y: p.y + m.top };
          }),
        })"""
    )


def test_phone_page_itself_never_scrolls(open_app):
    # iOS gives vertical swipes to a scrollable document, so the map would only
    # pan sideways: the page must not scroll or rubber-band, in any state.
    page = open_app(390, 844, init_scripts=[GEO_AND_SPEECH_MOCK])
    for el in ("document.documentElement", "document.body"):
        style = page.evaluate(f"() => {{ const s = getComputedStyle({el}); return [s.overflowY, s.overscrollBehaviorY]; }}")
        assert style == ["hidden", "none"], (el, style)
    route_via_ui(page)
    page.click("#start-ride")
    page.click("#end-ride")
    page.click("#sheet-toggle")
    page.click("#reset-route")
    assert page.evaluate("() => getComputedStyle(document.documentElement).overflowY") == "hidden"


def test_phone_portrait_map_pans_vertically_and_stays_near_sf(open_app):
    # The map ends at the sheet's top: a canvas running on under the sheet
    # counted the hidden strip against the pan limit, which then allowed no
    # north/south panning in portrait at city zoom.
    page = open_app(390, 844, init_scripts=[GEO_AND_SPEECH_MOCK])
    assert rect(page, "#map")["bottom"] <= rect(page, "#sidebar")["top"] + 1
    page.evaluate("() => map.jumpTo({ center: [-122.44, 37.765], zoom: 12 })")
    for dy in (250, -250):
        before = page.evaluate("() => map.getCenter().lat")
        page.evaluate(f"() => map.panBy([0, {dy}], {{ duration: 0 }})")
        after = page.evaluate("() => map.getCenter().lat")
        assert abs(after - before) > 0.01, (dy, before, after)
    # ...but a long fling still stops close to the city.
    page.evaluate("() => map.panBy([0, 20000], { duration: 0 })")
    assert page.evaluate("() => map.getBounds().getSouth()") >= 37.67
    page.evaluate("() => map.panBy([20000, 0], { duration: 0 })")
    assert page.evaluate("() => map.getBounds().getEast()") <= -122.32


def test_phone_steps_drawer_leaves_the_picked_step_in_view(open_app):
    # On a phone the drawer sits over the map: it must leave a band of map
    # above it, and a picked step must be drawn and framed in that band.
    page = open_app(390, 844, init_scripts=[GEO_AND_SPEECH_MOCK])
    route_via_ui(page)
    page.click("#show-steps")
    # A real route has dozens of steps: pad the list so the drawer is as
    # tall as its CSS lets it get.
    page.evaluate(
        """() => { const ol = document.getElementById('steps');
             for (let i = 0; i < 40; i++) ol.insertAdjacentHTML('beforeend', '<li>filler step</li>'); }"""
    )
    drawer = rect(page, "#steps-drawer")
    map_box = rect(page, "#map")
    assert drawer["top"] - map_box["top"] >= (map_box["bottom"] - map_box["top"]) * 0.4, (drawer, map_box)

    page.locator("#steps li").nth(1).click()
    page.wait_for_timeout(100)
    page.wait_for_function("() => !map.isMoving()")
    hl = page.evaluate(
        """() => {
          const f = map.querySourceFeatures('pev-step-hl');
          const s = state.route.steps[state.activeStep];
          const m = map.getContainer().getBoundingClientRect();
          const pts = (s.geometry || [s.turn_point]).map((c) => { const p = map.project(c); return [p.x + m.left, p.y + m.top]; });
          return { drawn: f.length, pts };
        }"""
    )
    assert hl["drawn"] > 0
    for x, y in hl["pts"]:
        assert map_box["top"] <= y <= drawer["top"], (y, drawer["top"])
        assert 0 <= x <= 390


def test_phone_end_ride_reachable_with_steps_open_and_a_step_picked(open_app):
    page = open_app(390, 844, init_scripts=[GEO_AND_SPEECH_MOCK])
    route_via_ui(page)
    page.click("#start-ride")
    fix_at(page, 0, 0)
    page.click("#ride-steps")
    page.evaluate(
        """() => { const ol = document.getElementById('steps');
             for (let i = 0; i < 40; i++) ol.insertAdjacentHTML('beforeend', '<li>filler step</li>'); }"""
    )
    # Mid-ride the camera carries the follow padding: picking a step must
    # still bring it into the map left between banner and drawer.
    assert page.evaluate("() => map.getPadding().top") > 0
    # On a long real route the follow padding is large (the rider sits low
    # under a tall banner), and the camera is usually mid-animation following
    # the rider: stand in for both.
    page.evaluate(
        "() => map.easeTo({ padding: { top: 420, right: 0, bottom: 120, left: 0 }, duration: 5000 })"
    )
    page.wait_for_timeout(200)
    page.locator("#steps li").nth(1).click()
    page.wait_for_timeout(100)
    page.wait_for_function("() => !map.isMoving()")
    top = rect(page, "#ride-banner")["bottom"]
    bottom = rect(page, "#steps-drawer")["top"]
    pts = page.evaluate(
        """() => { const s = state.route.steps[state.activeStep];
             const m = map.getContainer().getBoundingClientRect();
             return (s.geometry || [s.turn_point]).map((c) => map.project(c).y + m.top); }"""
    )
    for y in pts:
        assert top <= y <= bottom, (y, top, bottom)

    page.click("#end-ride", timeout=3000)
    assert page.evaluate("() => ride.active") is False
    # ...and the drawer gets out of the way of the route overview.
    assert not page.is_visible("#steps-drawer")


def test_phone_route_fit_keeps_both_pins_above_the_sheet(open_app):
    page = open_app(390, 844, is_mobile=True, has_touch=True, device_scale_factor=2)
    # A north-south route (A -> D -> G): its height, not its width, sets the
    # zoom, so the sheet's height matters.
    set_trip(page, end=(0, -200))
    page.wait_for_function("() => document.querySelectorAll('#steps li').length > 0", timeout=15000)

    def assert_pins_clear():
        sheet_top = rect(page, "#sidebar")["top"]
        for pin in framed_route(page)["pins"]:
            assert pin["top"] >= 0 and pin["left"] >= 0 and pin["right"] <= 390, pin
            assert pin["bottom"] <= sheet_top, f"pin {pin} hidden behind the sheet (top {sheet_top})"

    assert_pins_clear()
    # The sheet grows after the fit (a long warning wrapping onto more lines):
    # the overview is fitted again rather than left half under the sheet.
    sheet_before = rect(page, "#sidebar")["top"]
    page.evaluate(
        """() => document.getElementById('route-summary').insertAdjacentHTML('beforeend',
             '<div class="warn">' + 'A very long warning that wraps. '.repeat(8) + '</div>')"""
    )
    assert rect(page, "#sidebar")["top"] < sheet_before - 40
    assert_pins_clear()


def test_desktop_steps_drawer_never_covers_the_route(open_app):
    page = open_app(1280, 800, init_scripts=[GEO_AND_SPEECH_MOCK])
    route_via_ui(page)
    assert page.is_visible("#steps-drawer")  # opens with the route on desktop
    d = rect(page, "#steps-drawer")
    framed = framed_route(page)
    for p in framed["path"]:
        assert not (d["left"] <= p["x"] <= d["right"] and d["top"] <= p["y"] <= d["bottom"]), p
    for pin in framed["pins"]:
        overlap_x = min(pin["right"], d["right"]) - max(pin["left"], d["left"])
        overlap_y = min(pin["bottom"], d["bottom"]) - max(pin["top"], d["top"])
        assert overlap_x <= 0 or overlap_y <= 0, f"pin {pin} under the drawer {d}"

    # Riding: the drawer starts shut and the sidebar's Start ride is gone;
    # the ride bar's Steps button brings the drawer back.
    page.click("#start-ride")
    assert not page.is_visible("#steps-drawer")
    assert not page.is_visible("#start-ride")
    # The ride bar (bottom-left) pads the bottom, the banner the top; neither
    # pushes the rider sideways.
    pad = page.evaluate("() => mapPadding()")
    assert pad["left"] == pad["right"] == 40
    assert pad["top"] > 40 and pad["bottom"] > 40
    page.click("#ride-steps")
    assert page.is_visible("#steps-drawer")
    page.click("#end-ride")
    assert page.is_visible("#start-ride")


def test_use_my_location_reports_failures_in_page(page):
    page.evaluate(
        "() => { navigator.geolocation.getCurrentPosition = (ok, fail) => setTimeout(() => fail({ code: 1 }), 50); }"
    )
    page.click("#use-location")
    page.wait_for_function("() => document.getElementById('trip-status').textContent.includes('permission')")
    assert page.eval_on_selector("#trip-status", "el => el.classList.contains('error')")
    assert page.text_content("#start-label").startswith("Not set")


# ------------------------------------------------------- park near destination


def _set_trip_m(page, start, end, end_label="Cafe"):
    slon, slat = m_to_lonlat(*start)
    elon, elat = m_to_lonlat(*end)
    page.evaluate(
        f"""() => {{
          setStart({{ kind: 'coords', lat: {slat}, lon: {slon}, label: 'Test start' }});
          setEnd({{ kind: 'coords', lat: {elat}, lon: {elon}, label: '{end_label}' }});
        }}"""
    )


def _chips(page):
    return page.evaluate(
        """() => [...document.querySelectorAll('#park-near .park-chip')].map((b) => ({
             kind: b.dataset.kind, text: b.textContent.trim(), disabled: b.disabled,
             pressed: b.getAttribute('aria-pressed') === 'true' }))"""
    )


def test_park_near_chips_route_to_parking_and_back(page):
    assert not page.is_visible("#park-near")
    # Destination 10 m short of F: the synthetic BikeLink locker sits on F,
    # the charger on A (~220 m away); there are no racks.
    _set_trip_m(page, (0, -100), (190, -100))
    page.wait_for_function("() => state.route")
    chips = {c["kind"]: c for c in _chips(page)}
    assert "ft" in chips["locker"]["text"] and not chips["locker"]["disabled"]
    assert not chips["charger"]["disabled"]
    assert chips["rack"]["disabled"] and "none within 1 mi" in chips["rack"]["text"]

    page.click('.park-chip[data-kind="locker"]')
    page.wait_for_function("() => state.route && state.route.end_label !== 'Cafe'")
    assert page.evaluate("() => [state.endRef.poi, state.parkFor.label, state.parkKind]") == ["locker", "Cafe", "locker"]
    assert "Then walk" in page.text_content("#route-summary")
    assert page.is_visible(".goal-ring")
    assert page.evaluate("() => map.getSource('pev-walk')._data.features.length") == 1
    assert {c["kind"]: c["pressed"] for c in _chips(page)}["locker"] is True

    # The same chip again: back to riding all the way.
    page.click('.park-chip[data-kind="locker"]')
    page.wait_for_function("() => state.endRef.label === 'Cafe' && !state.parkFor")
    assert not page.is_visible(".goal-ring")
    assert page.evaluate("() => map.getSource('pev-walk')._data.features.length") == 0

    # A new destination drops the parking choice; Reset clears the row.
    page.click('.park-chip[data-kind="charger"]')
    page.wait_for_function("() => state.parkKind === 'charger'")
    _set_trip_m(page, (0, -100), (100, -100), end_label="Elsewhere")
    assert page.evaluate("() => [state.parkFor, state.parkKind, state.endRef.label]") == [None, None, "Elsewhere"]
    page.click("#reset-route")
    assert not page.is_visible("#park-near")


def test_no_park_chips_when_the_destination_is_a_parking_spot(page):
    lon, lat = m_to_lonlat(200, -100)
    page.evaluate(
        f"() => setEnd({{ kind: 'coords', lat: {lat}, lon: {lon}, label: 'Bike rack', poi: 'rack' }})"
    )
    assert not page.is_visible("#park-near")


def test_arrival_mentions_the_walk_to_the_real_destination(ride_page):
    page = ride_page
    _set_trip_m(page, (0, -100), (190, -100))
    page.wait_for_function("() => state.route")
    page.click('.park-chip[data-kind="locker"]')
    page.wait_for_function("() => state.parkKind === 'locker' && state.route.end_label !== 'Cafe'")
    page.click("#start-ride")
    fix_at(page, 200, -100)
    page.wait_for_function("() => ride.arrived")
    assert "Cafe is" in page.text_content("#ride-instruction")
    assert any("Cafe is" in t and "feet" in t for t in page.evaluate("() => window.__spoken"))


# ------------------------------------------------- phones: start from my location

# Headless Chrome reports a fine pointer even with touch emulation; the app
# decides "phone" from (pointer: coarse), so phone tests answer that query.
COARSE_POINTER = """
(() => {
  const real = window.matchMedia.bind(window);
  window.matchMedia = (q) => (q === "(pointer: coarse)" ? { ...real(q), matches: true, media: q } : real(q));
})();
"""


def test_phone_start_defaults_to_my_location_when_a_destination_is_picked(open_app):
    page = open_app(390, 844, init_scripts=[COARSE_POINTER, GEO_AND_SPEECH_MOCK], is_mobile=True, has_touch=True)
    # Nothing is asked at launch (permission not granted yet): the start reads
    # as "your location" and the destination is the row asked for first.
    assert page.evaluate("() => state.startRef") is None
    assert "Your location" in page.text_content("#start-label")
    assert page.evaluate("() => document.getElementById('end-row').classList.contains('needs-input')")
    # Picking a destination fills the start from the device and routes.
    lon, lat = m_to_lonlat(0, 0)
    page.evaluate(f"() => __emitFix({lon}, {lat})")
    elon, elat = m_to_lonlat(200, -100)
    page.evaluate(f"() => setEnd({{ kind: 'coords', lat: {elat}, lon: {elon}, label: 'Somewhere' }})")
    page.wait_for_function("() => state.startRef && state.route")
    assert page.evaluate("() => state.startRef.label") == "My location"


def test_phone_start_from_location_never_overrides_a_chosen_start(open_app):
    page = open_app(390, 844, init_scripts=[COARSE_POINTER, GEO_AND_SPEECH_MOCK], is_mobile=True, has_touch=True)
    # No fix arrives: the lookup fails, the rider is told, and can set a start.
    elon, elat = m_to_lonlat(200, -100)
    page.evaluate(f"() => setEnd({{ kind: 'coords', lat: {elat}, lon: {elon}, label: 'Somewhere' }})")
    page.wait_for_function("() => !document.getElementById('trip-status').classList.contains('hidden') && !locating")
    assert page.evaluate("() => state.startRef") is None
    assert page.evaluate("() => document.getElementById('start-row').classList.contains('needs-input')")
    slon, slat = m_to_lonlat(0, -100)
    page.evaluate(f"() => setStart({{ kind: 'coords', lat: {slat}, lon: {slon}, label: 'Chosen' }})")
    page.wait_for_function("() => state.route")
    assert page.evaluate("() => state.startRef.label") == "Chosen"


def test_phone_with_location_allowed_fills_the_start_at_launch(open_app):
    lon, lat = m_to_lonlat(0, 0)
    page = open_app(
        390, 844, init_scripts=[COARSE_POINTER], is_mobile=True, has_touch=True,
        permissions=["geolocation"], geolocation={"latitude": lat, "longitude": lon},
    )
    page.wait_for_function("() => state.startRef", timeout=10000)
    assert page.evaluate("() => state.startRef.label") == "My location"
    assert page.is_hidden("#trip-status")


def test_desktop_never_fills_the_start_by_itself(open_app):
    lon, lat = m_to_lonlat(0, 0)
    page = open_app(permissions=["geolocation"], geolocation={"latitude": lat, "longitude": lon})
    elon, elat = m_to_lonlat(200, -100)
    page.evaluate(f"() => setEnd({{ kind: 'coords', lat: {elat}, lon: {elon}, label: 'Somewhere' }})")
    page.wait_for_timeout(500)
    assert page.evaluate("() => state.startRef") is None


def test_mopeds_park_at_chargers_or_motorcycle_parking(page):
    _set_trip_m(page, (0, -100), (190, -100))
    page.wait_for_function("() => state.route")
    assert [c["kind"] for c in _chips(page)] == ["charger", "locker", "rack"]
    page.click('.park-chip[data-kind="locker"]')
    page.wait_for_function("() => state.parkKind === 'locker'")
    # Switching to a moped: the locker no longer fits, so the route goes back
    # to the real destination; lockers and racks give way to motorcycle parking.
    page.select_option("#vehicle", "moped")
    page.wait_for_function("() => !state.parkKind && state.endRef.label === 'Cafe'")
    assert [c["kind"] for c in _chips(page)] == ["charger", "moto"]
    assert not page.is_visible(".goal-ring")
    page.click('.park-chip[data-kind="moto"]')
    page.wait_for_function("() => state.parkKind === 'moto' && state.route.end_label !== 'Cafe'")
    assert page.evaluate("() => state.endRef.label") == "Motorcycle parking · Side St"
    # Back on a scooter: the motorcycle spot doesn't fit, the bike tiles return.
    page.select_option("#vehicle", "scooter")
    page.wait_for_function("() => !state.parkKind")
    assert [c["kind"] for c in _chips(page)] == ["charger", "locker", "rack"]


def test_map_pins_are_the_parking_kinds_that_fit_the_vehicle(page):
    kinds = """() => [...new Set(map.getSource('pev-pois')._data.features.map((f) => f.properties.kind))].sort()"""
    page.wait_for_function("() => map.getSource('pev-pois')")
    # Motorcycle parking shows for every vehicle; racks/lockers not for mopeds.
    assert page.evaluate(kinds) == ["charger", "locker", "moto"]  # the synthetic bundle has no racks
    page.select_option("#vehicle", "moped")
    assert page.evaluate(kinds) == ["charger", "moto"]
    # Tapping a motorcycle site offers to ride there.
    lon, lat = m_to_lonlat(100, -100)
    page.evaluate(f"() => map.jumpTo({{ center: [{lon}, {lat}], zoom: 17 }})")
    page.wait_for_function("() => !map.isMoving() && map.loaded()")
    page.wait_for_timeout(300)
    pt = page.evaluate(f"() => map.project([{lon}, {lat}])")
    box = page.locator("#map").bounding_box()
    page.mouse.click(box["x"] + pt["x"], box["y"] + pt["y"])
    page.wait_for_selector(".maplibregl-popup .bubble-title")
    assert page.text_content(".maplibregl-popup .bubble-title") == "Motorcycle parking"
    assert "3 spaces · free" in page.text_content(".maplibregl-popup .bubble-meta")
    page.select_option("#vehicle", "ebike_c3")
    assert page.evaluate(kinds) == ["charger", "locker", "moto"]


# ------------------------------------------------ location: precise, then coarse

def _flaky_geo(precise_code, coarse_code=None):
    """Geolocation whose precise request fails with `precise_code` and whose
    coarse retry fails with `coarse_code` (None: succeeds at node A)."""
    lon, lat = m_to_lonlat(0, 0)
    return f"""
(() => {{
  window.__geoCalls = [];
  const fail = (cb, code) => setTimeout(() => cb({{ code, message: 'CoreLocationProvider: kCLErrorLocationUnknown' }}), 0);
  const geo = {{
    getCurrentPosition(ok, err, opts) {{
      window.__geoCalls.push(!!(opts && opts.enableHighAccuracy));
      if (opts && opts.enableHighAccuracy) return fail(err, {precise_code});
      if ({'null' if coarse_code is None else coarse_code} !== null) return fail(err, {'null' if coarse_code is None else coarse_code});
      setTimeout(() => ok({{ coords: {{ longitude: {lon}, latitude: {lat}, accuracy: 80 }}, timestamp: Date.now() }}), 0);
    }},
    watchPosition() {{ return 1; }},
    clearWatch() {{}},
  }};
  Object.defineProperty(navigator, 'geolocation', {{ value: geo, configurable: true }});
}})();
"""


def test_location_unavailable_retries_with_a_coarse_fix(open_app):
    page = open_app(init_scripts=[_flaky_geo(2)])
    page.click("#use-location")
    page.wait_for_function("() => state.startRef")
    assert page.evaluate("() => state.startRef.label") == "My location"
    assert page.evaluate("() => window.__geoCalls") == [True, False]  # precise, then coarse
    assert page.is_hidden("#trip-status")


def test_location_failure_explains_what_to_check(open_app):
    page = open_app(init_scripts=[_flaky_geo(3, 2)])  # timeout, then unavailable
    page.click("#use-location")
    page.wait_for_selector("#trip-status.error")
    text = page.text_content("#trip-status")
    assert "Location Services" in text and "Wi-Fi" in text
    assert "CoreLocation" not in text  # never the raw platform message
    assert page.evaluate("() => state.startRef") is None


def test_location_denied_is_not_retried(open_app):
    page = open_app(init_scripts=[_flaky_geo(1)])
    page.click("#use-location")
    page.wait_for_selector("#trip-status.error")
    assert "permission" in page.text_content("#trip-status").lower()
    assert page.evaluate("() => window.__geoCalls") == [True]


def test_parking_walk_follows_the_streets(page):
    # The synthetic charger is on A; the destination is just short of F. The
    # walk goes along the streets (A -> B -> C -> F or A -> D -> E -> F, ~300 m),
    # not the ~220 m straight line.
    _set_trip_m(page, (0, -100), (190, -100))
    page.wait_for_function("() => state.route")
    page.click('.park-chip[data-kind="charger"]')
    page.wait_for_function("() => state.parkKind === 'charger' && walk && walk.key === walkKey()")
    coords = page.evaluate("() => map.getSource('pev-walk')._data.features[0].geometry.coordinates")
    assert len(coords) >= 4  # turns at the corners, not a straight line
    assert 280 < page.evaluate("() => parkWalkM()") < 320
    page.wait_for_function("() => /min\\)/.test(document.querySelector('#route-summary .park-note').textContent)")
    assert "Then walk" in page.text_content("#route-summary .park-note")



# --------------------------------------------- QA fixes: #25, #34, #35, #36


def test_point_off_the_network_shows_a_plain_message(page):
    # #25: a destination in the water used to print coordinates and node
    # distances.
    _set_trip_m(page, (0, -100), (3000, 3000), end_label="The Bay")
    page.wait_for_function("() => /isn't near a street/.test(document.getElementById('route-summary').textContent)")
    text = page.text_content("#route-summary")
    assert "farther than" not in text and "node" not in text
    assert not re.search(r"-?\d+\.\d{4,}", text)


def test_same_start_and_destination_is_already_there(page):
    # #35: no "0 ft · 1 min · 1 steps", no Start ride.
    _set_trip_m(page, (0, -100), (0, -100), end_label="Same spot")
    page.wait_for_function("() => state.route")
    assert "You're already there" in page.text_content("#route-summary")
    assert not page.is_visible("#start-ride")
    _set_trip_m(page, (0, -100), (190, -100))
    page.wait_for_function("() => state.route && state.route.distance_m > 1")
    assert page.is_visible("#start-ride")
    assert re.search(r"\d+ steps?", page.text_content("#route-summary"))
    assert "1 steps" not in page.text_content("#route-summary")


def _slow_geo(delay_ms, succeed=True):
    lon, lat = m_to_lonlat(0, 0)
    return f"""
(() => {{
  window.__geoOpts = [];
  const geo = {{
    getCurrentPosition(ok, err, opts) {{
      window.__geoOpts.push(opts);
      setTimeout(() => {succeed and "ok({ coords: { longitude: " + str(lon) + ", latitude: " + str(lat) + ", accuracy: 20 }, timestamp: Date.now() })" or "err({ code: 3, message: 'timeout' })"}, {delay_ms});
    }},
    watchPosition() {{ return 1; }},
    clearWatch() {{}},
  }};
  Object.defineProperty(navigator, 'geolocation', {{ value: geo, configurable: true }});
}})();
"""


def test_location_gives_up_within_about_12_seconds(open_app):
    # #34: two attempts of 6 s each (was 10 s + 15 s).
    page = open_app(init_scripts=[_slow_geo(50, succeed=False)])
    page.click("#use-location")
    page.wait_for_selector("#trip-status.error")
    opts = page.evaluate("() => window.__geoOpts")
    assert [o["timeout"] for o in opts] == [6000, 6000]


def test_picking_a_start_clears_the_location_message_and_wins(open_app):
    # #34: the timeout message stayed up after a route was set another way,
    # and a late location answer could overwrite the rider's own pick.
    page = open_app(init_scripts=[_slow_geo(50, succeed=False)])
    page.click("#use-location")
    page.wait_for_selector("#trip-status.error")
    slon, slat = m_to_lonlat(0, -100)
    page.evaluate(f"() => setStart({{ kind: 'coords', lat: {slat}, lon: {slon}, label: 'Chosen' }})")
    assert page.is_hidden("#trip-status")

    page2 = open_app(init_scripts=[_slow_geo(800, succeed=True)])
    page2.click("#use-location")
    page2.evaluate(f"() => setStart({{ kind: 'coords', lat: {slat}, lon: {slon}, label: 'Chosen' }})")
    page2.wait_for_timeout(1200)  # the lookup answers after the pick
    assert page2.evaluate("() => state.startRef.label") == "Chosen"
    assert page2.is_hidden("#trip-status")
    assert page2.is_enabled("#use-location")


def test_places_outside_the_city_are_labeled_and_not_pickable(page):
    # #36
    page.fill("#search", "Test Bakery")
    page.wait_for_selector("#search-results .result.outside")
    outside = page.locator("#search-results .result.outside")
    assert "Outside service area" in outside.text_content()
    assert outside.locator("button").evaluate_all("bs => bs.every((b) => b.disabled)")
    inside = page.locator("#search-results .result:not(.outside)").first
    assert inside.locator("button").evaluate_all("bs => bs.every((b) => !b.disabled)")


# ------------------------------------------------- QA fixes: #33, #37 (search)


def test_search_results_work_from_the_keyboard(page):
    # #33: arrow keys move a highlighted option, Enter picks it.
    page.fill("#search", "Test Bakery")
    page.wait_for_selector("#search-results [role=option]")
    assert page.get_attribute("#search", "role") == "combobox"
    assert page.get_attribute("#search-results", "role") == "listbox"
    page.press("#search", "ArrowDown")
    active = page.get_attribute("#search", "aria-activedescendant")
    assert active and page.get_attribute(f"#{active}", "aria-selected") == "true"
    assert "Test Bakery" in page.text_content(f"#{active}")
    # Desktop, nothing set: the Start row is the one asked for.
    page.press("#search", "Enter")
    page.wait_for_function("() => state.startRef")
    assert page.evaluate("() => state.startRef.label") == "Test Bakery"
    assert page.is_hidden("#search-results")
    # Shift+Enter fills the other row (the destination is asked for now).
    page.fill("#search", "Side Street")
    page.wait_for_selector("#search-results [role=option]")
    page.press("#search", "ArrowDown")
    page.press("#search", "Shift+Enter")
    assert page.evaluate("() => state.startRef.label") == "Side Street Deli"
    # ArrowUp from nothing wraps to the last option; Escape closes.
    page.fill("#search", "Test Bakery")
    page.wait_for_selector("#search-results [role=option]")
    page.press("#search", "ArrowUp")
    n = page.locator("#search-results [role=option]").count()
    assert page.get_attribute("#search", "aria-activedescendant") == f"search-opt-{n - 1}"
    page.press("#search", "Escape")
    assert page.is_hidden("#search-results")


def test_keyboard_pick_refuses_a_place_outside_the_city(page):
    page.fill("#search", "Test Bakery Oakland")
    page.wait_for_selector("#search-results .result.outside")
    for _ in range(page.locator("#search-results [role=option]").count()):
        page.press("#search", "ArrowDown")
        if "outside" in (page.get_attribute(f"#{page.get_attribute('#search', 'aria-activedescendant')}", "class") or ""):
            break
    page.press("#search", "Enter")
    assert page.evaluate("() => state.startRef") is None
    assert "outside San Francisco" in page.text_content("#search-status")


def test_no_matches_is_announced_once_and_footer_is_vehicle_neutral(page):
    # #37
    page.fill("#search", "zzqqxx123")
    page.wait_for_selector("#search-results:not(.hidden)")
    assert page.get_attribute("#search-results .result.muted", "aria-hidden") == "true"
    assert page.text_content("#search-status") == "No matches"
    footer = page.text_content("#sidebar footer")
    assert "Routes are guidance" in footer and "e-bike guidance" not in footer


# ------------------------------------------- #28: the CSP blocks nothing we use

CSP_WATCH = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) =>
  window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
"""


def test_content_security_policy_allows_everything_the_app_does(open_app):
    # Report-only still fires securitypolicyviolation: anything the policy
    # would block once enforced shows up here.
    page = open_app(init_scripts=[CSP_WATCH, GEO_AND_SPEECH_MOCK])
    route_via_ui(page)
    page.locator("#steps li").nth(1).click()  # the desktop drawer is open
    page.click("#start-ride")
    fix_at(page, 0, 0)
    page.click("#end-ride")
    page.evaluate("() => pev.hud(true)")  # injects its own <style>
    page.fill("#search", "Test Bakery")
    page.wait_for_selector("#search-results [role=option]")
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__csp") == []
    # The vendored copy is what ran.
    assert page.evaluate("() => typeof maplibregl.Map") == "function"
    loaded = page.evaluate("() => performance.getEntriesByType('resource').map((e) => e.name)")
    assert any("/vendor/maplibre-gl@4.7.1/maplibre-gl.js" in u for u in loaded)
    assert not any("jsdelivr" in u for u in loaded)



# ------------------------------------------------- map key, rider arrow, pins


def _legend(page):
    return page.evaluate("() => [...document.querySelectorAll('#map-legend-list li')].map((li) => li.textContent)")


def test_map_key_explains_the_colors_for_the_vehicle(page):
    assert page.is_visible("#map-legend-list")  # open by default on desktop
    items = _legend(page)
    for label in ("Your route", "Bike lane or path", "Start", "Destination", "EV charger",
                  "BikeLink locker", "Bike rack", "Motorcycle parking"):
        assert label in items, (label, items)
    # The swatches use the pins' own colors; chargers no longer share the
    # route's orange.
    colors = page.evaluate("() => PIN_COLORS")
    assert colors["charger"].lower() != "#ffb02e"
    paint = page.evaluate("() => JSON.stringify(map.getPaintProperty('pev-pois', 'circle-color'))")
    for c in colors.values():
        assert c in paint
    # A moped's key drops what its map drops.
    page.select_option("#vehicle", "moped")
    items = _legend(page)
    assert "Bike rack" not in items and "BikeLink locker" not in items and "Motorcycle parking" in items


def test_map_key_folds_and_remembers(page):
    page.click(".pev-legend-toggle")
    assert page.is_hidden("#map-legend-list")
    assert page.get_attribute(".pev-legend-toggle", "aria-expanded") == "false"
    page.reload()
    page.wait_for_selector("#map-loading", state="detached")
    assert page.is_hidden("#map-legend-list")
    page.click(".pev-legend-toggle")
    assert page.is_visible("#map-legend-list")


def test_map_key_starts_folded_on_a_phone_and_hides_while_riding(open_app):
    page = open_app(390, 844, init_scripts=[GEO_AND_SPEECH_MOCK])
    assert page.is_visible(".pev-legend-toggle") and page.is_hidden("#map-legend-list")
    route_via_ui(page)
    page.click("#start-ride")
    assert page.is_hidden(".pev-legend")


def test_rider_is_a_red_arrow_that_turns_with_the_heading(ride_page):
    page = ride_page
    route_via_ui(page)
    page.click("#start-ride")
    fix_at(page, 0, 0, heading=90, speed=5)
    page.wait_for_selector(".rider-puck svg path")
    assert page.get_attribute(".rider-puck svg path", "fill").lower() == "#e82127"
    assert page.evaluate("() => Math.round(riderMarker.getRotation())") == 90


# ------------------------------------------------------- QA round 2: #44, #45


def test_timeout_with_the_prompt_unanswered_says_to_answer_it(open_app):
    # #44: the browser's own prompt was never answered (permission "prompt").
    page = open_app(init_scripts=[_slow_geo(50, succeed=False)])
    page.click("#use-location")
    page.wait_for_selector("#trip-status.error")
    text = page.text_content("#trip-status")
    assert "answer your browser's location prompt" in text and "Timed out" not in text


def test_timeout_with_location_allowed_says_what_to_check(open_app):
    lon, lat = m_to_lonlat(0, 0)
    page = open_app(
        init_scripts=[_slow_geo(50, succeed=False)],
        permissions=["geolocation"], geolocation={"latitude": lat, "longitude": lon},
    )
    page.click("#use-location")
    page.wait_for_selector("#trip-status.error")
    text = page.text_content("#trip-status")
    assert "Location Services" in text and "Wi-Fi" in text


def test_end_ride_clears_a_step_picked_during_the_ride(ride_page):
    # #45: the step highlight (and the selected step) outlived the ride.
    page = ride_page
    route_via_ui(page)
    page.click("#start-ride")
    fix_at(page, 0, 0)
    page.click("#ride-steps")
    page.locator("#steps li").nth(1).click()
    page.wait_for_function("() => state.activeStep !== null")
    page.click("#end-ride")
    assert page.evaluate("() => state.activeStep") is None
    assert page.evaluate("() => map.getSource('pev-step-hl')._data.features.length") == 0
    assert page.locator("#steps li.active").count() == 0
    # On a desktop the steps list stays beside the map, as next to any route.
    assert page.is_visible("#steps-drawer")
