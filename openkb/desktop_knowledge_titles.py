"""Shared title normalization for Desktop Concept and Entity knowledge."""

from __future__ import annotations


def normalize_knowledge_title(value: str) -> tuple[str, str]:
    """Return the display and lookup forms used by Desktop knowledge records."""
    display_title = " ".join(value.split())
    return display_title, display_title.casefold()
