// Issue #2: show address (when present) under each search hit so same-name
// places are distinguishable. Redefines runSearch; app.js handlers call it
// by name at keyup time, so this override is what runs.

function resultSubtitle(r) {
  const cat = r.category || "Place";
  const addr = (r.address || "").trim();
  if (!addr) return cat;
  // Address-kind hits already use the street line as the title — don't repeat.
  if (addr.toLowerCase() === (r.text || "").toLowerCase()) return cat;
  return cat + " · " + addr;
}

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
      sub.className = "result-sub";
      sub.textContent = resultSubtitle(r);
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
