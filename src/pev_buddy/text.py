"""Text matching helpers."""

from __future__ import annotations

import unicodedata


def fold(text: str | None) -> str:
    """Case- and accent-insensitive form for matching: 'Café Encore' and
    'CAFE ENCORE' both become 'cafe encore'. Letters are decomposed (NFKD) and
    their combining marks dropped, so riders can type with or without accents
    whichever way the data spells a name."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
