"""Named-POI search over Overture's SF places dataset (businesses, parks, ...)."""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path

import numpy as np
import orjson

from . import config
from .place_fixes import fix_places
from .text import fold


def format_category(cat: str | None) -> str:
    if not cat:
        return "Place"
    return cat.replace("_", " ").title()


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
        # Hand-added landmarks, known-misplaced records dropped, typos fixed
        # (see place_fixes).
        rows = fix_places(rows)
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
            grouped.setdefault(fold(r[1]), []).append(i)
        self.cat_vocab = cat_vocab
        self.cat_code = cat_code
        self.by_name: dict[str, tuple[int, ...]] = {k: tuple(v) for k, v in grouped.items()}
        # Sorted folded names: prefix matches are a bisect, not a scan of
        # every name on each keystroke.
        self.sorted_names: list[str] = sorted(self.by_name)

    def __len__(self) -> int:
        return len(self.names)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = fold((query or "").strip())
        if not q:
            return []
        scored: list[tuple[int, int]] = []
        for i in self.by_name.get(q, ()):
            scored.append((100, i))
        want = limit * 2
        if len(scored) < want:
            lo = bisect_left(self.sorted_names, q)
            hi = bisect_left(self.sorted_names, q + "\uffff")
            for name in self.sorted_names[lo:hi]:
                if name != q:  # exact already scored
                    scored.extend((80, i) for i in self.by_name[name])
        # Substring matches ("encore" in "cafe encore") need a scan: only for
        # queries of 3+ characters, and only when prefixes left room.
        if len(scored) < want and len(q) >= 3:
            for name, idxs in self.by_name.items():
                if q in name and not name.startswith(q):
                    scored.extend((60, i) for i in idxs)
        scored.sort(key=lambda t: (-t[0], len(self.names[t[1]]), self.names[t[1]].casefold()))
        out: list[dict] = []
        for score, i in scored[:limit]:
            out.append(
                {
                    "name": self.names[i],
                    "category": format_category(self.cat_vocab[int(self.cat_code[i])]),
                    "address": self.addresses[i],
                    "lon": float(self.lon[i]),
                    "lat": float(self.lat[i]),
                    "score": score,
                }
            )
        return out
