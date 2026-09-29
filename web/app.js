"use strict";

const SF_CENTER = [-122.4194, 37.7749];
// Users who prefer reduced motion get instant map jumps instead of animated
// flyTo/fitBounds transitions.
const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)").matches;
const FLY = REDUCED_MOTION ? { duration: 0 } : {};
// City of San Francisco extent — the map can't pan or zoom out beyond this.
const SF_BOUNDS = [
  [-122.52, 37.7],
  [-122.35, 37.84],
];

const map = new maplibregl.Map({
  container: "map",
  style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  center: SF_CENTER,
  zoom: 12.5,
  // maxBounds clamps zoom-out at the city's extent; minZoom is kept well below
  // that so it never overrides the bounds on wide viewports.
  minZoom: 9,
  maxBounds: SF_BOUNDS,
  attributionControl: false,
});
map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
map.addControl(new maplibregl.NavigationControl(), "top-right");
// MapLibre labels the canvas just "Map" — give assistive tech a real summary.
// Keyboard pan/zoom (+/-/arrows) is built into the canvas itself.
map.getCanvas().setAttribute(
  "aria-label",
  "Interactive map of San Francisco showing EV chargers, BikeLink lockers, and the current route"
);

const state = {
  startRef: null, // {kind, station_id?, query?, lat, lon, label}
  endRef: null,
  routing: false,
  routeShown: false, // a route is currently drawn on the map
  activeStep: null, // index of the step highlighted on the map
  vehicle: "scooter", // CA vehicle type id (from /api/vehicles)
};

// ---------------------------------------------------------------- vehicle type

let vehicleTypes = []; // {id, label, description, routable, speed_limit_mph}

// The dropdown and its live description both come from /api/vehicles, which
// serves config.VEHICLE_TYPES — the single source of truth for vehicle types.
async function loadVehicleTypes() {
  const res = await fetch("/api/vehicles");
  if (!res.ok) return;
  const data = await res.json();
  vehicleTypes = data.vehicles;
  const sel = document.getElementById("vehicle");
  for (const v of data.vehicles) {
    const opt = document.createElement("option");
    opt.value = v.id;
    opt.textContent = v.label;
    sel.appendChild(opt);
  }
  state.vehicle = data.default;
  sel.value = data.default;
  syncVehicleDesc();
}

function syncVehicleDesc() {
  const v = vehicleTypes.find((x) => x.id === state.vehicle);
  document.getElementById("vehicle-desc").textContent = v ? v.description : "";
}

document.getElementById("vehicle").addEventListener("change", (e) => {
  state.vehicle = e.target.value;
  syncVehicleDesc();
  maybeRoute();
});
loadVehicleTypes();

const fmtMi = (m) => `${(m / 1609.34).toFixed(1)} mi`;
const fmtMin = (s) => `${Math.max(1, Math.round(s / 60))} min`;

// ---------------------------------------------------------------- map layers

map.on("load", async () => {
  let roads, stations, bikelink;
  try {
    roads = await (await fetch("/data/roads.geojson")).json();
    stations = await (await fetch("/data/stations.geojson")).json();
    const bikelinkRes = await fetch("/data/bikelink.geojson");
    bikelink = bikelinkRes.ok
      ? await bikelinkRes.json()
      : { type: "FeatureCollection", features: [] };
  } catch {
    const el = document.getElementById("map-loading");
    if (el) el.textContent = "Map data failed to load — reload to try again.";
    return;
  }

  map.addSource("pev-roads", { type: "geojson", data: roads });
  map.addLayer({
    id: "pev-roads",
    type: "line",
    source: "pev-roads",
    paint: {
      "line-color": [
        "match",
        ["get", "class"],
        "cycleway", "#35d07f",
        "path", "#35d07f",
        "track", "#35d07f",
        "footway", "#4a5d70",
        "residential", "#5d7086",
        "living_street", "#5d7086",
        "service", "#526275",
        "unclassified", "#526275",
        "tertiary", "#46566a",
        "#333f4e",
      ],
      "line-width": [
        "match",
        ["get", "class"],
        "cycleway", 2.6,
        "path", 2.6,
        "track", 2.2,
        1.4,
      ],
      "line-opacity": 0.85,
    },
  });

  map.addSource("pev-route-casing", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "pev-route-casing",
    type: "line",
    source: "pev-route-casing",
    paint: { "line-color": "#0c141d", "line-width": 10, "line-opacity": 0.95 },
  });
  map.addSource("pev-route", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "pev-route",
    type: "line",
    source: "pev-route",
    paint: { "line-color": "#ffb02e", "line-width": 5.5, "line-opacity": 0.98 },
  });

  // Highlight for the step selected in the turn-by-turn list.
  map.addSource("pev-step-hl", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "pev-step-hl",
    type: "line",
    source: "pev-step-hl",
    paint: { "line-color": "#ffffff", "line-width": 7, "line-opacity": 0.95 },
  });
  map.addSource("pev-step-dot", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "pev-step-dot",
    type: "circle",
    source: "pev-step-dot",
    paint: {
      "circle-radius": 7,
      "circle-color": "#ffffff",
      "circle-stroke-width": 3,
      "circle-stroke-color": "#ffb02e",
    },
  });

  map.addSource("pev-stations", { type: "geojson", data: stations, promoteId: "id" });
  map.addLayer({
    id: "pev-stations",
    type: "circle",
    source: "pev-stations",
    paint: {
      "circle-radius": 9,
      "circle-color": "#ffb02e",
      "circle-stroke-width": 2.5,
      "circle-stroke-color": "#0c141d",
    },
  });

  // BikeLink lockers — a different color from chargers (blue vs. amber).
  map.addSource("pev-bikelink", { type: "geojson", data: bikelink, promoteId: "id" });
  map.addLayer({
    id: "pev-bikelink",
    type: "circle",
    source: "pev-bikelink",
    paint: {
      "circle-radius": 8,
      "circle-color": "#4da3ff",
      "circle-stroke-width": 2.5,
      "circle-stroke-color": "#0c141d",
    },
  });

  const clickable = ["pev-bikelink", "pev-stations"];
  map.on("mousemove", (e) => {
    const over = map.queryRenderedFeatures(e.point, { layers: clickable }).length > 0;
    map.getCanvas().style.cursor = over ? "pointer" : "";
  });

  const loading = document.getElementById("map-loading");
  if (loading) loading.remove();
});

function emptyFC() {
  return { type: "FeatureCollection", features: [] };
}

const startMarker = new maplibregl.Marker({ element: makeDot("#35d07f") });
const endMarker = new maplibregl.Marker({ element: makeDot("#ffb02e") });

function makeDot(color) {
  const el = document.createElement("div");
  // Decorative — the sidebar start/destination labels carry the same info as text.
  el.setAttribute("aria-hidden", "true");
  el.style.cssText = `width:14px;height:14px;border-radius:50%;background:${color};border:2.5px solid #101820;box-shadow:0 1px 6px rgba(0,0,0,.6);`;
  return el;
}

// ---------------------------------------------------------------- stations

// Build a details "bubble" for a charger and let the user navigate to it.
function showStationPopup(s) {
  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, offset: 14 }).setLngLat([s.lon, s.lat]);

  const card = document.createElement("div");
  card.className = "bubble";

  const title = document.createElement("div");
  title.className = "bubble-title";
  title.textContent = s.name || "Charging station";

  const meta = document.createElement("div");
  meta.className = "bubble-meta";
  const bits = [];
  if (s.brand) bits.push(s.brand);
  if (s.address) bits.push(s.address);
  if (s.phone) bits.push(s.phone);
  if (bits.length) meta.textContent = bits.join(" · ");
  else meta.textContent = "No details available";

  card.append(title, meta);

  if (s.website) {
    const a = document.createElement("a");
    a.className = "bubble-link";
    a.href = s.website;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Charger info ↗";
    card.appendChild(a);
  }

  const btn = document.createElement("button");
  btn.className = "bubble-route";
  btn.textContent = "Navigate here";
  btn.addEventListener("click", () => {
    popup.remove();
    const label = s.name || "Charging station";
    // Prefer the station id; fall back to coordinates if it is unavailable.
    const ref = s.id
      ? { kind: "station", station_id: s.id, lat: s.lat, lon: s.lon, label }
      : { kind: "coords", lat: s.lat, lon: s.lon, label };
    setEnd(ref);
    promptStartIfMissing();
  });
  card.appendChild(btn);

  popup.setDOMContent(card).addTo(map);
  return popup;
}

// MapLibre stringifies array properties on rendered features, so a list that
// went out as JSON comes back as a JSON string — normalize either shape.
function asStringArray(v) {
  if (Array.isArray(v)) return v;
  if (typeof v === "string") {
    try {
      const a = JSON.parse(v);
      return Array.isArray(a) ? a : [];
    } catch {
      return [];
    }
  }
  return [];
}

// Build a details "bubble" for a BikeLink locker and let the user navigate to it.
function showBikeLinkPopup(b) {
  b.access_devices = asStringArray(b.access_devices);
  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, offset: 14 }).setLngLat([b.lon, b.lat]);

  const card = document.createElement("div");
  card.className = "bubble";

  const title = document.createElement("div");
  title.className = "bubble-title";
  title.textContent = b.name || "BikeLink locker";

  const meta = document.createElement("div");
  meta.className = "bubble-meta";
  const bits = [];
  if (b.facility_type) bits.push(b.facility_type);
  if (b.address) bits.push(b.address);
  if (b.num_spaces) bits.push(`${b.num_spaces} ${b.facility_type === "eLocker" ? "lockers" : "spaces"}`);
  if (bits.length) meta.textContent = bits.join(" · ");
  else meta.textContent = "No details available";

  card.append(title, meta);

  if (b.access_devices && b.access_devices.length) {
    const access = document.createElement("div");
    access.className = "bubble-meta";
    access.textContent = `Access: ${b.access_devices.join(", ")}`;
    card.appendChild(access);
  }

  const btn = document.createElement("button");
  btn.className = "bubble-route";
  btn.textContent = "Navigate here";
  btn.addEventListener("click", () => {
    popup.remove();
    const label = b.name || "BikeLink locker";
    const ref = b.id
      ? { kind: "bikelink", station_id: b.id, lat: b.lat, lon: b.lon, label }
      : { kind: "coords", lat: b.lat, lon: b.lon, label };
    setEnd(ref);
    promptStartIfMissing();
  });
  card.appendChild(btn);

  popup.setDOMContent(card).addTo(map);
  return popup;
}

// ---------------------------------------------------------------- trip refs

const DEFAULT_START_LABEL = "Not set — search or click the map";
const DEFAULT_END_LABEL = "Not set — search, click the map, or a pin";

function setStart(ref) {
  state.startRef = ref;
  document.getElementById("start-label").textContent = ref.label;
  startMarker.setLngLat([ref.lon, ref.lat]).addTo(map);
  maybeRoute();
}

function setEnd(ref) {
  state.endRef = ref;
  document.getElementById("end-label").textContent = ref.label;
  endMarker.setLngLat([ref.lon, ref.lat]).addTo(map);
  maybeRoute();
}

function maybeRoute() {
  if (state.startRef && state.endRef) computeRoute();
}

// Clear everything: start/end, markers, route line, step highlight, and the
// steps drawer.
function resetTrip() {
  state.startRef = null;
  state.endRef = null;
  state.routeShown = false;
  state.activeStep = null;
  startMarker.remove();
  endMarker.remove();
  document.getElementById("start-label").textContent = DEFAULT_START_LABEL;
  document.getElementById("end-label").textContent = DEFAULT_END_LABEL;
  if (map.getSource("pev-route")) map.getSource("pev-route").setData(emptyFC());
  if (map.getSource("pev-route-casing")) map.getSource("pev-route-casing").setData(emptyFC());
  clearStepHighlight();
  document.getElementById("route-summary").classList.add("hidden");
  document.getElementById("steps-drawer").classList.add("hidden");
  syncStepsToggle();
}
document.getElementById("reset-route").addEventListener("click", resetTrip);

// While a route is on the map but the steps drawer is hidden, offer a way
// back to the steps.
function syncStepsToggle() {
  const drawerHidden = document.getElementById("steps-drawer").classList.contains("hidden");
  const toggle = document.getElementById("show-steps");
  toggle.classList.toggle("hidden", !(state.routeShown && drawerHidden));
  toggle.setAttribute("aria-expanded", String(!drawerHidden));
}

function openStepsDrawer(moveFocus) {
  document.getElementById("steps-drawer").classList.remove("hidden");
  syncStepsToggle();
  if (moveFocus) document.getElementById("steps-close").focus();
}

function closeStepsDrawer(moveFocus) {
  document.getElementById("steps-drawer").classList.add("hidden");
  syncStepsToggle();
  // Return focus to the toggle that reopens the drawer, if it is on screen.
  const toggle = document.getElementById("show-steps");
  if (moveFocus && !toggle.classList.contains("hidden")) toggle.focus();
}

document.getElementById("show-steps").addEventListener("click", () => openStepsDrawer(true));

function useMyLocation(which) {
  if (!navigator.geolocation) return alert("Geolocation is not available in this browser.");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const ref = { kind: "coords", lat: pos.coords.latitude, lon: pos.coords.longitude, label: "My location" };
      if (which === "end") setEnd(ref);
      else setStart(ref);
      map.flyTo({ center: [pos.coords.longitude, pos.coords.latitude], zoom: 14, ...FLY });
    },
    () => alert("Could not get your location."),
    { enableHighAccuracy: true, timeout: 8000 }
  );
}
document.getElementById("use-location").addEventListener("click", () => useMyLocation("start"));
document.getElementById("use-location-end").addEventListener("click", () => useMyLocation("end"));

// ---------------------------------------------------------------- search

const searchInput = document.getElementById("search");
const searchResults = document.getElementById("search-results");
const searchStatus = document.getElementById("search-status");
let searchTimer = null;

function showSearchResults() {
  searchResults.classList.remove("hidden");
  searchInput.setAttribute("aria-expanded", "true");
}

function hideSearchResults() {
  searchResults.classList.add("hidden");
  searchInput.setAttribute("aria-expanded", "false");
  searchStatus.textContent = "";
}

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) {
    hideSearchResults();
    return;
  }
  searchTimer = setTimeout(() => runSearch(q), 300);
});

// Escape anywhere inside the search area closes the dropdown and returns
// focus to the input.
document.querySelector(".search").addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || searchResults.classList.contains("hidden")) return;
  hideSearchResults();
  searchInput.focus();
});

async function runSearch(q) {
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=8`);
    if (!res.ok) throw new Error();
    const items = await res.json();
    if (!items.length) {
      searchResults.innerHTML = `<div class="result muted">No matches</div>`;
      showSearchResults();
      searchStatus.textContent = "No matches";
      return;
    }
    searchResults.innerHTML = "";
    for (const r of items) {
      const div = document.createElement("div");
      div.className = "result";
      const label = document.createElement("span");
      label.textContent = r.text;
      const sub = document.createElement("small");
      sub.textContent = r.category || "Place";
      div.append(label, sub);
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:6px;margin-top:6px;";
      for (const [text, kind] of [["Start", "start"], ["Destination", "end"]]) {
        const b = document.createElement("button");
        b.className = "mini";
        b.textContent = text;
        b.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const ref = {
            kind: r.kind === "station" || r.kind === "bikelink" ? r.kind : "point",
            station_id: r.station_id || null,
            label: r.text,
            lat: r.lat,
            lon: r.lon,
          };
          if (kind === "start") setStart(ref);
          else setEnd(ref);
          map.flyTo({ center: [r.lon, r.lat], zoom: 15, ...FLY });
          hideSearchResults();
        });
        row.appendChild(b);
      }
      div.appendChild(row);
      div.addEventListener("click", () => {
        map.flyTo({ center: [r.lon, r.lat], zoom: 15, ...FLY });
        hideSearchResults();
      });
      searchResults.appendChild(div);
    }
    showSearchResults();
    searchStatus.textContent = `${items.length} result${items.length === 1 ? "" : "s"}`;
  } catch {
    hideSearchResults();
  }
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".search")) hideSearchResults();
});

// map click: a charger shows its bubble; empty space sets start (if a
// destination exists) or destination. While a route is on the map, clicks do
// nothing (pan only) so the route can't be accidentally edited.
map.on("click", (e) => {
  if (state.routeShown) return;
  const feats = map.queryRenderedFeatures(e.point, { layers: ["pev-bikelink", "pev-stations"] });
  if (feats.length) {
    const f = feats[0];
    const p = f.properties || {};
    if (f.layer && f.layer.id === "pev-bikelink") {
      showBikeLinkPopup({
        id: f.id,
        name: p.name,
        facility_type: p.facility_type,
        address: p.address,
        num_spaces: p.num_spaces,
        access_devices: p.access_devices,
        lat: f.geometry.coordinates[1],
        lon: f.geometry.coordinates[0],
      });
    } else {
      showStationPopup({
        id: f.id,
        name: p.name,
        brand: p.brand,
        address: p.address,
        phone: p.phone,
        website: p.website,
        lat: f.geometry.coordinates[1],
        lon: f.geometry.coordinates[0],
      });
    }
    return;
  }
  const ref = { kind: "coords", lat: e.lngLat.lat, lon: e.lngLat.lng, raw: true, label: `${e.lngLat.lat.toFixed(5)}, ${e.lngLat.lng.toFixed(5)}` };
  if (state.endRef) setStart(ref);
  else {
    setEnd(ref);
    promptStartIfMissing();
  }
});

// When a destination is chosen but there is no start, nudge the user.
function promptStartIfMissing() {
  if (state.startRef || !state.endRef) return;
  const summary = document.getElementById("route-summary");
  summary.classList.remove("hidden");
  summary.innerHTML = `<span class="warn">Pick a start point</span> — use “Use my location”, the search box, or click the map.`;
}

// ---------------------------------------------------------------- routing

async function computeRoute() {
  if (state.routing) return;
  state.routing = true;
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
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    if (!state.startRef || !state.endRef) return; // trip was reset mid-request
    renderRoute(data);
  } catch (err) {
    state.routeShown = false;
    summary.innerHTML = `<span class="warn">⚠ ${escapeHtml(err.message)}</span>`;
    closeStepsDrawer(false);
  } finally {
    state.routing = false;
    btns.forEach((b) => (b.disabled = false));
  }
}

function refToApi(ref) {
  if (!ref) return {};
  if ((ref.kind === "station" || ref.kind === "bikelink") && ref.station_id) {
    return { station_id: ref.station_id };
  }
  const out = { lat: ref.lat, lon: ref.lon };
  if (ref.label && !ref.raw) out.label = ref.label;
  return out;
}

function renderRoute(data) {
  state.routeShown = true;
  const fc = {
    type: "Feature",
    geometry: { type: "LineString", coordinates: data.path },
  };
  map.getSource("pev-route").setData({ type: "FeatureCollection", features: [fc] });
  map.getSource("pev-route-casing").setData({ type: "FeatureCollection", features: [fc] });
  if (data.path.length) {
    const b = map.getBounds();
    for (const [lon, lat] of data.path) b.extend([lon, lat]);
    map.fitBounds(b, { padding: { top: 60, bottom: 60, left: 380, right: 60 }, duration: REDUCED_MOTION ? 0 : 600 });
  }

  const summary = document.getElementById("route-summary");
  let html = `<b>${fmtMi(data.distance_m)}</b> · ${fmtMin(data.duration_s)} · ${data.steps.length} steps<br>`;
  html += `<span style="color:var(--muted)">From ${escapeHtml(data.start_label || "start")} to ${escapeHtml(data.end_label || "destination")}</span>`;
  for (const w of data.warnings || []) html += `<div class="warn">⚠ ${escapeHtml(w)}</div>`;
  summary.innerHTML = html;

  clearStepHighlight();
  const ol = document.getElementById("steps");
  ol.innerHTML = "";
  for (const s of data.steps) {
    const li = document.createElement("li");
    if (s.maneuver === "arrive") li.className = "arrive";
    const idx = document.createElement("span");
    idx.className = "idx";
    idx.textContent = s.index;
    const txt = document.createElement("span");
    txt.className = "txt";
    const main = document.createElement("span");
    main.textContent = s.instruction;
    txt.appendChild(main);
    // Show the road name whenever the instruction does not already contain it
    // (e.g. the arrival step).
    if (s.road && !s.instruction.includes(s.road)) {
      const sub = document.createElement("span");
      sub.className = "txt-road";
      sub.textContent = s.road;
      txt.appendChild(sub);
    }
    const dist = document.createElement("span");
    dist.className = "dist";
    dist.textContent = s.maneuver === "depart" || s.maneuver === "arrive" ? "" : fmtMi(s.distance_m);
    li.append(idx, txt, dist);
    li.addEventListener("click", () => selectStep(li, s));
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.setAttribute("aria-pressed", "false");
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectStep(li, s);
      }
    });
    ol.appendChild(li);
  }
  document.getElementById("steps-title").textContent = "Turn-by-turn";
  openStepsDrawer(false);
}

// ---------------------------------------------------------------- step highlight

function clearStepHighlight() {
  state.activeStep = null;
  if (map.getSource("pev-step-hl")) map.getSource("pev-step-hl").setData(emptyFC());
  if (map.getSource("pev-step-dot")) map.getSource("pev-step-dot").setData(emptyFC());
  document.querySelectorAll("#steps li.active").forEach((li) => {
    li.classList.remove("active");
    li.setAttribute("aria-pressed", "false");
  });
}

// Click a step in the list: highlight the segment it covers on the map
// (click the same step again to clear).
function selectStep(li, step) {
  if (state.activeStep === step.index) {
    clearStepHighlight();
    return;
  }
  clearStepHighlight();
  state.activeStep = step.index;
  li.classList.add("active");
  li.setAttribute("aria-pressed", "true");

  const line =
    step.geometry && step.geometry.length > 1
      ? [{ type: "Feature", geometry: { type: "LineString", coordinates: step.geometry } }]
      : [];
  const dots = [];
  if (step.turn_point) dots.push({ type: "Feature", geometry: { type: "Point", coordinates: step.turn_point } });
  else if (step.geometry && step.geometry.length)
    dots.push({ type: "Feature", geometry: { type: "Point", coordinates: step.geometry[step.geometry.length - 1] } });

  map.getSource("pev-step-hl").setData({ type: "FeatureCollection", features: line });
  map.getSource("pev-step-dot").setData({ type: "FeatureCollection", features: dots });

  if (step.geometry && step.geometry.length > 1) {
    const b = map.getBounds();
    for (const c of step.geometry) b.extend(c);
    map.fitBounds(b, { padding: { top: 80, bottom: 80, left: 380, right: 340 }, maxZoom: 17, duration: REDUCED_MOTION ? 0 : 500 });
  } else if (step.turn_point) {
    map.flyTo({ center: step.turn_point, zoom: 16, duration: REDUCED_MOTION ? 0 : 500 });
  }
}

document.getElementById("steps-close").addEventListener("click", () => closeStepsDrawer(true));
document.getElementById("steps-drawer").addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeStepsDrawer(true);
});

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
