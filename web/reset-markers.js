// Issue #7: Reset must clear start/end markers and abort in-flight /api/route.
// Loaded after app.js. Click handlers for Reset/Cancel rebound below (app.js
// passed resetTrip by reference). setStart/setEnd/computeRoute are looked up
// by name at call time, so redefining them here is enough.

// AbortController for the in-flight /api/route — Reset aborts so a late
// response cannot revive markers/route UI (issue #7).
let routeAbort = null;

// MapLibre's Marker.remove() is usually enough; also detach the element if it
// was left in the DOM so Reset cannot leave a ghost pin (issue #7).
function removeMarker(marker) {
  marker.remove();
  const el = marker.getElement();
  if (el && el.parentNode) el.parentNode.removeChild(el);
}

function clearEndpointMarkers() {
  removeMarker(startMarker);
  removeMarker(endMarker);
}

function setStart(ref) {
  state.startRef = ref;
  document.getElementById("start-label").textContent = ref.label;
  startMarker.setLngLat([ref.lon, ref.lat]).addTo(map);
  // Keep the destination marker in lockstep with endRef (issue #7): after
  // Reset, a start-only update must not leave a stale orange end pin.
  if (!state.endRef) removeMarker(endMarker);
  maybeRoute();
}

function setEnd(ref) {
  state.endRef = ref;
  document.getElementById("end-label").textContent = ref.label;
  endMarker.setLngLat([ref.lon, ref.lat]).addTo(map);
  if (!state.startRef) removeMarker(startMarker);
  maybeRoute();
}

// Clear everything: start/end, markers, route line, step highlight, and the
// steps drawer. Also abort any in-flight /api/route so a late response cannot
// resurrect route UI after Reset.
function resetTrip() {
  if (routeAbort) {
    routeAbort.abort();
    routeAbort = null;
  }
  state.startRef = null;
  state.endRef = null;
  state.routeShown = false;
  state.activeStep = null;
  state.routing = false;
  clearEndpointMarkers();
  document.getElementById("start-label").textContent = DEFAULT_START_LABEL;
  document.getElementById("end-label").textContent = DEFAULT_END_LABEL;
  if (map.getSource("pev-route")) map.getSource("pev-route").setData(emptyFC());
  if (map.getSource("pev-route-casing")) map.getSource("pev-route-casing").setData(emptyFC());
  clearStepHighlight();
  document.getElementById("route-summary").classList.add("hidden");
  document.getElementById("steps-drawer").classList.add("hidden");
  syncStepsToggle();
  syncNavUI();
  // Leaving navigation mode: face north again and let the screen sleep.
  map.easeTo({ bearing: 0, ...FLY });
  releaseWakeLock();
}

async function computeRoute() {
  if (state.routing) return;
  state.routing = true;
  if (routeAbort) routeAbort.abort();
  routeAbort = new AbortController();
  const abort = routeAbort;
  const btns = document.querySelectorAll(".bubble-route");
  btns.forEach((b) => (b.disabled = true));
  const summary = document.getElementById("route-summary");
  summary.classList.remove("hidden");
  summary.textContent = "Finding your route…";

  const body = {
    start: refToApi(state.startRef),
    end: refToApi(state.endRef),
    vehicle: state.vehicle,
  };
  try {
    const res = await fetch("/api/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abort.signal,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    // Trip was reset (or start/end cleared) while the request was in flight.
    if (!state.startRef || !state.endRef || abort !== routeAbort) return;
    renderRoute(data);
  } catch (err) {
    if (err && err.name === "AbortError") return;
    state.routeShown = false;
    syncNavUI();
    summary.innerHTML = `<span class="warn">⚠ ${escapeHtml(err.message)}</span>`;
    closeStepsDrawer(false);
  } finally {
    // Only the active request may clear routing — a Reset-aborted call must
    // not clobber a newer computeRoute that started afterward.
    if (abort === routeAbort) {
      routeAbort = null;
      state.routing = false;
      btns.forEach((b) => (b.disabled = false));
    }
  }
}

// app.js bound the original resetTrip by reference — replace those buttons
// so clicks hit this module's resetTrip.
for (const id of ["reset-route", "cancel-route"]) {
  const btn = document.getElementById(id);
  if (!btn) continue;
  const next = btn.cloneNode(true);
  btn.replaceWith(next);
  next.addEventListener("click", resetTrip);
}
