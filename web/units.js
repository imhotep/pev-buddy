// Issue #5: SF UI is imperial-only. Redefine renderRoute (app.js looked up
// fmtMi via a const the overlay cannot replace) so totals, warnings (already
// imperial from the API), and step distances never show metres or "0.0 mi".

function renderRoute(data) {
  // Issue #5: imperial-only labels (ft under 0.1 mi); hide true zeros (no "0.0 mi").
  const imperialFmt = (m) => {
    if (!(m > 0)) return "";
    const miles = m / 1609.34;
    if (miles < 0.1) return `${Math.round(m / 0.3048)} ft`;
    return `${miles.toFixed(1)} mi`;
  };
  state.routeShown = true;
  syncNavUI();
  if (IS_MOBILE) {
    // Start tracking the rider and keep the screen on for the ride.
    if (geolocateControl) geolocateControl.trigger();
    acquireWakeLock();
  }
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
  let html = `<b>${imperialFmt(data.distance_m)}</b> · ${fmtMin(data.duration_s)} · ${data.steps.length} steps<br>`;
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
    dist.textContent = s.maneuver === "depart" || s.maneuver === "arrive" ? "" : imperialFmt(s.distance_m);
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
