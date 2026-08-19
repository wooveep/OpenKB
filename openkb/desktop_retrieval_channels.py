"""Canonical identities for Desktop's vectorless retrieval channels."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

DesktopRetrievalVariant = Literal[
    "fts",
    "structure_lexical",
    "wiki",
    "baseline",
    "local_graph",
]

DESKTOP_RETRIEVAL_VARIANT_ORDER: tuple[DesktopRetrievalVariant, ...] = (
    "fts",
    "structure_lexical",
    "wiki",
    "baseline",
    "local_graph",
)
DESKTOP_RETRIEVAL_VARIANTS: frozenset[DesktopRetrievalVariant] = frozenset(
    DESKTOP_RETRIEVAL_VARIANT_ORDER
)

_LEGACY_CHANNEL_ALIASES = {"page_tree": "structure_lexical"}


def normalize_retrieval_channel(value: str) -> str:
    """Map a persisted legacy identity to its canonical retrieval channel."""
    return _LEGACY_CHANNEL_ALIASES.get(value, value)


def normalize_retrieval_channels(values: Iterable[str]) -> tuple[str, ...]:
    """Canonicalize channel values with stable de-duplication."""
    return tuple(dict.fromkeys(normalize_retrieval_channel(value) for value in values))
