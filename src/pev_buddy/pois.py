"""Named-POI search over Overture's SF places dataset (businesses, parks, ...)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import orjson

from . import config
from .geo import haversine


def format_category(cat: str | None) -> str:
    if not cat:
        return "Place"
    return cat.replace("_", " ").title()


# Same-name POIs within this radius collapse to one hit (issue #2).
DEDUPE_M = 75.0


class POIIndex:
    """Case-insensitive name search over a compact [id, name, category, address, lon, lat] list.

    Exact > prefix > substring. Deliberately no fuzzy pass: with ~80k names it
    would dominate latency, and typed POI queries are overwhelmingly covered by
    the three tiers above.

    Rows are stored as parallel vocab-coded columns (a dict per place would
    roughly triple the memory for no behavior gain); the place id is dropped —
    nothing consumes it.
    """

    def __init__(self, path: Path | None = None):
        path = path or config.DATA_DIR / "places.json"
        rows = orjson.loads(Path(path).read_bytes())
        n = len(rows)
        self.names: list[str] = [r[1] for r in rows]
        self.addresses: list = [r[3] for r in rows]
        self.lon = np.array([r[4] for r in rows], dtype=np.float64)
        self.lat = np.array([r[5] for r in rows], dtype=np.float64)
        cat_vocab: list = [None]
        cat_index: dict = {None: 0}
        cat_code = np.empty(n, dtype=np.int32)
        grouped: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            c = cat_index.get(r[2])
            if c is None:
                c = len(cat_vocab)
                cat_vocab.append(r[2])
                cat_index[r[2]] = c
            cat_code[i] = c
            grouped.setdefault(r[1].casefold(), []).append(i)
        self.cat_vocab = cat_vocab
        self.cat_code = cat_code
        self.by_name: dict[str, tuple[int, ...]] = {k: tuple(v) for k, v in grouped.items()}

    def __len__(self) -> int:
        return len(self.names)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = (query or "").strip().casefold()
        if not q:
            return []
        scored: list[tuple[int, int]] = []
        for i in self.by_name.get(q, ()):
            scored.append((100, i))
        if len(scored) < limit * 2:
            for name, idxs in self.by_name.items():
                if name == q:
                    continue  # already scored as exact
                if name.startswith(q):
                    for i in idxs:
                        scored.append((80, i))
                elif q in name:
                    for i in idxs:
                        scored.append((60, i))
        # Prefer higher score, then entries with an address (issue #2 subtitles),
        # then shorter / A-Z names. Near-duplicate same-name pins are collapsed below.
        scored.sort(
            key=lambda t: (
                -t[0],
                0 if self.addresses[t[1]] else 1,
                len(self.names[t[1]]),
                self.names[t[1]].casefold(),
            )
        )
        out: list[dict] = []
        kept: list[tuple[str, float, float]] = []  # name_key, lon, lat
        for score, i in scored:
            name = self.names[i]
            name_key = name.casefold()
            lon, lat = float(self.lon[i]), float(self.lat[i])
            if any(
                name_key == kn and haversine(lon, lat, klon, klat) < DEDUPE_M
                for kn, klon, klat in kept
            ):
                continue
            kept.append((name_key, lon, lat))
            out.append(
                {
                    "name": name,
                    "category": format_category(self.cat_vocab[int(self.cat_code[i])]),
                    "address": self.addresses[i],
                    "lon": lon,
                    "lat": lat,
                    "score": score,
                }
            )
            if len(out) >= limit:
                break
        return out
