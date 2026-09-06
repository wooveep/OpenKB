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
DesktopEvaluationVariant = Literal[
    "fts",
    "structure_lexical",
    "wiki",
    "baseline",
    "local_graph",
    "document_page_tree",
    "catalog + document_page_tree",
    "navigator",
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
DESKTOP_EVALUATION_VARIANT_ORDER: tuple[DesktopEvaluationVariant, ...] = (
    *DESKTOP_RETRIEVAL_VARIANT_ORDER,
    "document_page_tree",
    "catalog + document_page_tree",
    "navigator",
)
DESKTOP_EVALUATION_VARIANTS: frozenset[DesktopEvaluationVariant] = frozenset(
    DESKTOP_EVALUATION_VARIANT_ORDER
)
CATALOG_RETRIEVAL_VARIANTS: frozenset[DesktopEvaluationVariant] = frozenset(
    ("wiki", "baseline", "local_graph", "catalog + document_page_tree")
)
PAGE_TREE_EVALUATION_VARIANTS: frozenset[DesktopEvaluationVariant] = frozenset(
    ("document_page_tree", "catalog + document_page_tree")
)
RETRIEVAL_CHANNELS_BY_VARIANT: dict[DesktopEvaluationVariant, tuple[str, ...]] = {
    "fts": ("fts",),
    "structure_lexical": ("structure_lexical",),
    "wiki": ("wiki", "knowledge_source", "catalog"),
    "baseline": ("fts", "structure_lexical", "wiki", "knowledge_source", "catalog"),
    "local_graph": (
        "fts",
        "structure_lexical",
        "wiki",
        "knowledge_source",
        "catalog",
        "knowledge_graph",
    ),
    "document_page_tree": ("structure_lexical", "document_page_tree"),
    "catalog + document_page_tree": (
        "structure_lexical",
        "catalog",
        "document_page_tree",
    ),
    "navigator": (
        "fts",
        "structure_lexical",
        "wiki",
        "knowledge_source",
        "catalog",
        "knowledge_graph",
        "document_page_tree",
        "knowledge_navigation_source_window",
    ),
}

_LEGACY_CHANNEL_ALIASES = {"page_tree": "structure_lexical"}


def normalize_retrieval_channel(value: str) -> str:
    """Map a persisted legacy identity to its canonical retrieval channel."""
    return _LEGACY_CHANNEL_ALIASES.get(value, value)


def normalize_retrieval_channels(values: Iterable[str]) -> tuple[str, ...]:
    """Canonicalize channel values with stable de-duplication."""
    return tuple(dict.fromkeys(normalize_retrieval_channel(value) for value in values))
