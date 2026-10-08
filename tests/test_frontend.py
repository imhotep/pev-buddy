"""Browser tests for the web UI.

These run the real frontend in headless Chrome (system Chrome via Playwright's
``channel="chrome"``, so no browser download is needed) against a live uvicorn
server bound to a throwaway port. They are skipped automatically when
Playwright or Chrome is not installed.
"""

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
            off: [offRouteCount(0, 60, 5), offRouteCount(2, 60, 5), offRouteCount(2, 10, 5), offRouteCount(2, 60, 200)],
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
    assert r["off"] == [1, 3, 0, 2]
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
