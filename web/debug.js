// PEV Buddy developer tools, for the browser console (on a phone: Safari
// Web Inspector). Nothing here runs until called; `pev.help()` lists it all.
//
//   pev.hud()            toggle the on-screen debug panel: a live readout
//                        (viewport/scroll, map gesture handling, the last
//                        touch gesture, recent camera calls, console
//                        warnings) and buttons for the tools below, with
//                        their results shown in the panel and a Copy button,
//                        since a phone has no console. Remembered across
//                        reloads (localStorage).
//   pev.route()          the current route: summary plus a steps table
//   pev.fix(lat, lon)    feed ride mode one GPS fix (as if the phone moved)
//   pev.drive(mph)       ride the current route with simulated fixes;
//                        pev.stop() ends it
//   pev.state()          snapshot of app, ride and camera state
//   pev.resetApp()       drop the service worker + caches and reload (when
//                        a phone is stuck on an old build)
//
// No console at hand (a phone, the installed app)? Tap the "PEV Buddy" title
// five times, or add `?debug` to the URL.
(() => {
  const HUD_KEY = "pev-debug-hud";
  const store = {
    get: () => {
      try {
        return localStorage.getItem(HUD_KEY) === "1";
      } catch {
        return false;
      }
    },
    set: (on) => {
      try {
        if (on) localStorage.setItem(HUD_KEY, "1");
        else localStorage.removeItem(HUD_KEY);
      } catch {
        /* private mode: the HUD just won't be remembered */
      }
    },
  };

  const desc = (el) =>
    !el
      ? "null"
      : el === document
        ? "document"
        : el.tagName.toLowerCase() +
          (el.id ? "#" + el.id : "") +
          (typeof el.className === "string" && el.className ? "." + el.className.trim().split(/\s+/)[0] : "");

  // ------------------------------------------------------------------ HUD

  // The panel: a toolbar of the console tools, the live readout, and an
  // output box for what the tools return. Minimized, only the grip and the
  // expand button stay, so it can still be moved out of the map's way.
  const PANEL_CSS = `
#pev-debug-hud { position: fixed; left: 4px; top: calc(env(safe-area-inset-top) + 4px); z-index: 99999;
  width: min(92vw, 430px); max-height: 70vh; display: flex; flex-direction: column; gap: 4px; padding: 6px;
  background: rgba(0, 0, 0, 0.88); color: #7f7; font: 10px/1.3 ui-monospace, Menlo, monospace;
  border: 1px solid #2f4f2f; border-radius: 8px; }
#pev-debug-hud.min { width: auto; }
#pev-debug-hud.min > :not(.pev-dbg-bar),
#pev-debug-hud.min .pev-dbg-bar > :not(.pev-dbg-min):not(.pev-dbg-grip) { display: none; }
#pev-debug-hud .pev-dbg-bar { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; padding-right: 34px; }
#pev-debug-hud.min .pev-dbg-bar { padding-right: 0; }
/* Close sits in the panel's top-right corner, apart from the tools. */
#pev-debug-hud .pev-dbg-close { position: absolute; top: 6px; right: 6px; min-width: 28px; padding: 4px 0; }
#pev-debug-hud button, #pev-debug-hud input { font: 11px ui-monospace, Menlo, monospace; color: #cfe; background: #12261a;
  border: 1px solid #2f5f3f; border-radius: 5px; padding: 4px 7px; min-height: 28px; }
#pev-debug-hud input { width: 3.2em; padding: 4px; }
#pev-debug-hud button:active { background: #234; }
#pev-debug-hud pre { margin: 0; white-space: pre-wrap; overflow: auto; }
#pev-debug-hud .pev-dbg-out { color: #cde; border-top: 1px solid #2f4f2f; padding-top: 4px; max-height: 32vh; }
#pev-debug-hud .pev-dbg-readout { pointer-events: none; }
#pev-debug-hud .pev-dbg-grip { cursor: grab; touch-action: none; user-select: none; -webkit-user-select: none;
  color: #9c9; font-size: 16px; line-height: 1; padding: 4px 6px; border-radius: 5px; }
#pev-debug-hud.dragging .pev-dbg-grip { cursor: grabbing; background: #234; }`;

  let output = null; // the panel's output box
  function show(title, value) {
    const text = typeof value === "string" ? value : JSON.stringify(value, null, 1);
    if (output) {
      output.textContent = `${title}\n${text}`;
      output.hidden = false;
    }
    return value;
  }

  function buildPanel() {
    if (!document.getElementById("pev-debug-css")) {
      const style = document.createElement("style");
      style.id = "pev-debug-css";
      style.textContent = PANEL_CSS;
      document.head.appendChild(style);
    }
    const panel = document.createElement("div");
    panel.id = "pev-debug-hud";
    panel.setAttribute("role", "region");
    panel.setAttribute("aria-label", "Debug panel");
    const bar = document.createElement("div");
    bar.className = "pev-dbg-bar";
    const button = (label, title, fn, cls) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.title = title;
      if (cls) b.className = cls;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        fn();
      });
      bar.appendChild(b);
      return b;
    };
    const mph = document.createElement("input");
    mph.type = "number";
    mph.min = "1";
    mph.max = "200";
    mph.value = "12";
    mph.title = "Simulated speed (mph)";
    mph.setAttribute("aria-label", "Simulated speed in mph");
    const grip = document.createElement("span");
    grip.className = "pev-dbg-grip";
    grip.textContent = "⠿";
    grip.title = "Drag to move the panel";
    grip.setAttribute("aria-hidden", "true");
    bar.appendChild(grip);
    button("🐞", "Minimize / expand the panel", () => {
      panel.classList.toggle("min");
      placePanel(panel); // a wider panel may now run off the screen
    }, "pev-dbg-min");
    button("▶ Drive", "Ride the current route with simulated GPS", () => show("drive", drive(Number(mph.value) || 12)));
    bar.appendChild(mph);
    button("■ Stop", "Stop the simulated drive", () => show("stop", stop() || "Stopped."));
    button("Route", "Current route summary and steps", () => show("route", route(true)));
    button("State", "App, ride and camera snapshot", () => show("state", snapshot()));
    button("Copy", "Copy the readout and the last result", () => copy(panel));
    button("Reset app", "Drop the service worker and caches, then reload", () => resetApp());
    button("✕", "Close the panel", () => window.pev.hud(false), "pev-dbg-close");
    const readout = document.createElement("pre");
    readout.className = "pev-dbg-readout";
    output = document.createElement("pre");
    output.className = "pev-dbg-out";
    output.hidden = true;
    panel.append(bar, readout, output);
    document.body.appendChild(panel);
    makeDraggable(panel, grip);
    placePanel(panel, loadPos());
    return { panel, readout };
  }

  // Moving the panel: drag the grip (finger or mouse). Kept on screen, and
  // remembered across reloads.
  const POS_KEY = "pev-debug-pos";
  function loadPos() {
    try {
      return JSON.parse(localStorage.getItem(POS_KEY) || "null");
    } catch {
      return null;
    }
  }
  function savePos(pos) {
    try {
      localStorage.setItem(POS_KEY, JSON.stringify(pos));
    } catch {
      /* private mode: not remembered */
    }
  }
  function placePanel(panel, pos) {
    const r = panel.getBoundingClientRect();
    const want = pos || { x: r.left, y: r.top };
    const x = Math.max(0, Math.min(want.x, innerWidth - r.width));
    const y = Math.max(0, Math.min(want.y, innerHeight - Math.min(r.height, 40)));
    panel.style.left = `${Math.round(x)}px`;
    panel.style.top = `${Math.round(y)}px`;
    return { x, y };
  }
  function makeDraggable(panel, grip) {
    let start = null;
    grip.addEventListener("pointerdown", (e) => {
      const r = panel.getBoundingClientRect();
      start = { dx: e.clientX - r.left, dy: e.clientY - r.top, id: e.pointerId };
      grip.setPointerCapture(e.pointerId);
      panel.classList.add("dragging");
      e.preventDefault();
    });
    grip.addEventListener("pointermove", (e) => {
      if (!start || e.pointerId !== start.id) return;
      placePanel(panel, { x: e.clientX - start.dx, y: e.clientY - start.dy });
    });
    const end = (e) => {
      if (!start || e.pointerId !== start.id) return;
      start = null;
      panel.classList.remove("dragging");
      const r = panel.getBoundingClientRect();
      savePos({ x: r.left, y: r.top });
    };
    grip.addEventListener("pointerup", end);
    grip.addEventListener("pointercancel", end);
    const onResize = () => placePanel(panel);
    window.addEventListener("resize", onResize);
    panelUndo.push(() => window.removeEventListener("resize", onResize));
  }
  let panelUndo = [];

  async function copy(panel) {
    const text = [...panel.querySelectorAll("pre")].filter((p) => !p.hidden).map((p) => p.textContent).join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      show("copy", "Copied the readout and the last result.");
    } catch {
      // No clipboard (http, or permission): leave it selected to copy by hand.
      const range = document.createRange();
      range.selectNodeContents(panel.querySelector(".pev-dbg-readout"));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      show("copy", "Clipboard unavailable: the readout is selected, copy it by hand.");
    }
  }

  let hud = null; // { el, timer, undo: [] }

  function startHud() {
    if (hud) return;
    const { panel, readout } = buildPanel();
    const el = readout;
    const undo = [];
    const listen = (target, type, fn, opts) => {
      target.addEventListener(type, fn, opts);
      undo.push(() => target.removeEventListener(type, fn, opts));
    };

    // The last one-finger gesture: where it started, whether the map took
    // it (touchmove defaultPrevented), whether iOS cancelled it, what
    // scrolled instead, and how far the map moved.
    const g = { start: "-", moves: 0, prevented: 0, cancel: "no", scrolled: new Set(), mapMoved: "no", end: "-", c0: null };
    const opts = { capture: true, passive: true };
    listen(document, "touchstart", (e) => {
      if (e.touches.length === 1) {
        Object.assign(g, { moves: 0, prevented: 0, cancel: "no", scrolled: new Set(), mapMoved: "no", end: "-" });
        g.c0 = map.getCenter();
      }
      const t = e.touches[0];
      g.start = `${e.touches.length}t on ${desc(e.target)} @${Math.round(t.clientX)},${Math.round(t.clientY)}`;
    }, opts);
    listen(document, "touchmove", (e) => {
      g.moves++;
      setTimeout(() => e.defaultPrevented && g.prevented++);
    }, opts);
    listen(document, "touchend", () => {
      if (!g.c0) return;
      const c = map.getCenter();
      g.end = `dx=${Math.round((c.lng - g.c0.lng) * 88000)}m dy=${Math.round((c.lat - g.c0.lat) * 111000)}m`;
    }, opts);
    listen(document, "touchcancel", () => (g.cancel = "YES (iOS took it)"), opts);
    listen(document, "scroll", (e) => g.scrolled.add(desc(e.target)), opts);
    if (window.visualViewport) listen(visualViewport, "scroll", () => g.scrolled.add("visualViewport"));
    const onMove = (e) => (g.mapMoved = e.originalEvent ? "by finger" : "by code");
    map.on("movestart", onMove);
    undo.push(() => map.off("movestart", onMove));

    // Recent camera calls, with who made them.
    const calls = [];
    for (const k of ["easeTo", "jumpTo", "flyTo", "fitBounds", "setPadding", "rotateTo", "stop"]) {
      const orig = map[k];
      map[k] = function (...a) {
        const who = (new Error().stack || "")
          .split("\n")
          .slice(2, 4)
          .map((l) => (l.match(/at (\w+)/) || l.match(/^(\w+)@/) || [])[1] || "?")
          .join("<");
        calls.push(`${k} ${who}`);
        if (calls.length > 5) calls.shift();
        return orig.apply(this, a);
      };
      undo.push(() => (map[k] = orig));
    }

    // The last console warning or error (MapLibre reports refused camera
    // moves that way, e.g. "Map cannot fit within canvas").
    let lastWarn = "-";
    for (const k of ["warn", "error"]) {
      const orig = console[k];
      console[k] = (...a) => {
        lastWarn = `${k}: ${a.map(String).join(" ").slice(0, 90)}`;
        orig.apply(console, a);
      };
      undo.push(() => (console[k] = orig));
    }
    listen(window, "error", (ev) => (lastWarn = "ERR " + ev.message));

    const paint = () => {
      const vv = window.visualViewport;
      let active = "?";
      try {
        active = map.handlers._handlers.filter((h) => h.handler.isActive()).map((h) => h.handlerName).join(",") || "none";
      } catch {
        /* MapLibre internals moved */
      }
      const p = map.getPadding();
      const z = map.getZoom().toFixed(1);
      el.textContent = [
        `win ${innerWidth}x${innerHeight} vv ${vv ? `${Math.round(vv.width)}x${Math.round(vv.height)} s${vv.scale.toFixed(2)}` : "?"} scroll ${scrollY}`,
        `map ${map.getContainer().clientWidth}x${map.getContainer().clientHeight} z${z} pad ${Math.round(p.top)}/${Math.round(p.bottom)} dragPan ${map.dragPan.isEnabled()} active ${active}`,
        `body.${document.body.className || "-"} route ${!!state.route} ride ${ride.active}${ride.active ? ` next ${ride.next} off ${ride.offCount}` : ""}`,
        `gesture ${g.start}`,
        `  moves ${g.moves} prevented ${g.prevented} cancel ${g.cancel} scrolled ${[...g.scrolled].join(",") || "nothing"}`,
        `  map ${g.mapMoved} ${g.end}`,
        `warn ${lastWarn}`,
        ...calls.map((c) => "  " + c),
      ].join("\n");
    };
    hud = { el: panel, timer: setInterval(paint, 300), undo };
    paint();
  }

  function stopHud() {
    if (!hud) return;
    clearInterval(hud.timer);
    hud.undo.forEach((f) => f());
    hud.el.remove();
    hud = null;
    output = null;
    panelUndo.forEach((f) => f());
    panelUndo = [];
  }

  // ------------------------------------------------------------ GPS sim

  let driving = null;

  function fix(lat, lon, { accuracy = 8, heading = null, speed = null } = {}) {
    onRideFix({ coords: { latitude: lat, longitude: lon, accuracy, heading, speed }, timestamp: Date.now() });
  }

  function bearing([lon0, lat0], [lon1, lat1]) {
    const r = Math.PI / 180;
    const y = Math.sin((lon1 - lon0) * r) * Math.cos(lat1 * r);
    const x = Math.cos(lat0 * r) * Math.sin(lat1 * r) - Math.sin(lat0 * r) * Math.cos(lat1 * r) * Math.cos((lon1 - lon0) * r);
    return (Math.atan2(y, x) / r + 360) % 360;
  }

  function drive(mph = 12) {
    if (!state.route) return "No route: plan one first.";
    if (!ride.active) startRide();
    stop();
    const path = state.route.path;
    const mps = mph * 0.44704;
    let i = 0;
    let t = 0; // 0..1 along path[i] -> path[i + 1]
    driving = setInterval(() => {
      let budget = mps; // meters this second
      while (i < path.length - 1 && budget > 0) {
        const a = path[i];
        const b = path[i + 1];
        const len = Math.hypot((b[0] - a[0]) * 88000, (b[1] - a[1]) * 111000) || 1e-6;
        const left = (1 - t) * len;
        if (budget >= left) {
          budget -= left;
          i++;
          t = 0;
        } else {
          t += budget / len;
          budget = 0;
        }
      }
      if (i >= path.length - 1) {
        const end = path[path.length - 1];
        fix(end[1], end[0]);
        stop();
        return;
      }
      const a = path[i];
      const b = path[i + 1];
      fix(a[1] + (b[1] - a[1]) * t, a[0] + (b[0] - a[0]) * t, { heading: bearing(a, b), speed: mps });
    }, 1000);
    return `Driving at ${mph} mph; pev.stop() to end.`;
  }

  function stop() {
    if (driving) clearInterval(driving);
    driving = null;
  }

  // ------------------------------------------------------------- the rest

  function route(forPanel = false) {
    const r = state.route;
    if (!r) return "No route.";
    console.table(
      r.steps.map((s) => ({ step: s.index, instruction: s.instruction, ft: Math.round((s.distance_m || 0) * 3.28084), s: s.duration_s }))
    );
    return {
      ...(forPanel ? { steps: r.steps.map((s) => `${s.index}. ${s.instruction} (${Math.round((s.distance_m || 0) * 3.28084)} ft)`) } : {}),
      miles: +(r.distance_m / 1609.344).toFixed(2),
      minutes: Math.round(r.duration_s / 60),
      climb_ft: Math.round((r.climb_m || 0) * 3.28084),
      descent_ft: Math.round((r.descent_m || 0) * 3.28084),
      vehicle: r.vehicle,
      from: r.start_label,
      to: r.end_label,
      segment_stats: r.segment_stats,
      warnings: r.warnings,
    };
  }

  function snapshot() {
    const c = map.getCenter();
    return {
      start: state.startRef,
      end: state.endRef,
      routeShown: state.routeShown,
      activeStep: state.activeStep,
      ride: { ...ride, line: ride.line ? `${ride.line.length} pts` : null, positions: undefined },
      camera: { center: [+c.lng.toFixed(5), +c.lat.toFixed(5)], zoom: +map.getZoom().toFixed(2), bearing: Math.round(map.getBearing()), padding: map.getPadding() },
      build: document.querySelector('script[src*="app.js"]')?.getAttribute("src"),
    };
  }

  async function resetApp() {
    const regs = (await navigator.serviceWorker?.getRegistrations()) || [];
    await Promise.all(regs.map((r) => r.unregister()));
    const keys = (await caches?.keys()) || [];
    await Promise.all(keys.map((k) => caches.delete(k)));
    location.reload();
  }

  function help() {
    console.log(
      [
        "pev.hud()           toggle the on-screen debug panel (remembered); or tap the title 5x",
        "pev.route()         current route summary + steps table",
        "pev.fix(lat, lon)   feed ride mode one GPS fix",
        "pev.drive(mph=12)   ride the current route with simulated GPS; pev.stop() ends it",
        "pev.state()         app / ride / camera snapshot",
        "pev.resetApp()      drop service worker + caches, then reload",
      ].join("\n")
    );
  }

  window.pev = {
    help,
    hud(on) {
      const want = on === undefined ? !hud : !!on;
      store.set(want);
      if (want) startHud();
      else stopHud();
      return want ? "HUD on" : "HUD off";
    },
    route,
    fix,
    drive,
    stop,
    state: snapshot,
    resetApp,
  };

  if (store.get() || new URLSearchParams(location.search).has("debug")) startHud();

  // Five quick taps on the "PEV Buddy" title open the panel: the way in on a
  // phone, where there is no console to type pev.hud() into.
  let taps = [];
  document.querySelector("#sidebar header h1")?.addEventListener("click", () => {
    const now = Date.now();
    taps = [...taps.filter((t) => now - t < 2500), now];
    if (taps.length >= 5) {
      taps = [];
      window.pev.hud(true);
    }
  });
})();
