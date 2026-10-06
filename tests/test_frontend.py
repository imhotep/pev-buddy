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
def page(browser, live_server):
    pg = browser.new_page(viewport={"width": 1280, "height": 800})
    # Tests exercise the app, not the first-visit intro — pre-dismiss it.
    pg.add_init_script("localStorage.setItem('pev-welcome-dismissed', '1')")
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"{live_server}/", wait_until="networkidle")
    pg.wait_for_function("() => typeof setStart === 'function'")
    # Map data loaded: the loading overlay removes itself once layers are up.
    pg.wait_for_selector("#map-loading", state="detached", timeout=20000)
    yield pg
    pg.close()
    assert not errors, f"uncaught JS errors on page: {errors}"


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
    # Closing the drawer returns focus to the toggle that reopens the drawer...
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


def test_route_distances_are_imperial_without_zero_mi_steps(page):
    """Issue #5: summary/warnings/steps use imperial units; no 0.0 mi step labels."""
    route_via_ui(page)
    summary = page.text_content("#route-summary")
    assert " mi" in summary
    # Warnings (if any) must not show bare metres.
    assert " m " not in f" {summary} "
    dists = page.eval_on_selector_all(
        "#steps li .dist",
        "els => els.map(e => e.textContent.trim())",
    )
    assert dists, "expected step distance elements"
    for d in dists:
        assert d != "0.0 mi"
        if d:
            assert d.endswith(" mi") or d.endswith(" ft")
            assert not d.endswith(" m")
