"""Shared title normalization for Desktop Concept and Entity knowledge."""

from __future__ import annotations

import re
import unicodedata

_COMMON_PUNCTUATION = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "﹘": "-",
        "﹣": "-",
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "﹕": ":",
    }
)
_LATIN_PRODUCT_TITLE = re.compile(r"^[a-z0-9]+(?:[ _-]+[a-z0-9]+)*$")
_EXPLICIT_ALIAS_MARKERS = (
    "又称",
    "亦称",
    "简称",
    "全称",
    "also known as",
    "abbreviated as",
    "officially named",
    " aka ",
)


def normalize_knowledge_title(value: str) -> tuple[str, str]:
    """Return the display and lookup forms used by Desktop knowledge records."""
    display_title = " ".join(
        unicodedata.normalize("NFKC", value).translate(_COMMON_PUNCTUATION).split()
    )
    return display_title, display_title.casefold()


def controlled_latin_title_key(value: str) -> str:
    """Return a separator-insensitive Latin-name signal without merging mixed scripts."""
    lookup = normalize_knowledge_title(value)[1]
    if not _LATIN_PRODUCT_TITLE.fullmatch(lookup):
        return lookup
    return re.sub(r"[ _-]+", "", lookup)


def claim_explicitly_supports_alias(left_title: str, right_title: str, claim_text: str) -> bool:
    """Recognize an evidence-bound, explicit alias assertion between two supplied names."""
    left = normalize_knowledge_title(left_title)[1]
    right = normalize_knowledge_title(right_title)[1]
    claim = normalize_knowledge_title(claim_text)[1]
    return bool(
        left
        and right
        and left in claim
        and right in claim
        and any(marker in claim for marker in _EXPLICIT_ALIAS_MARKERS)
    )
