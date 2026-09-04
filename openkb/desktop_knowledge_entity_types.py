"""Code-owned Entity subtype ontology for new knowledge analysis."""

from __future__ import annotations

ENTITY_SUBTYPE_ONTOLOGY_VERSION = "openkb.entity-subtypes.v1"

ENTITY_SUBTYPES = frozenset(
    {
        "product",
        "organization",
        "service",
        "software_component",
        "hardware_component",
        "named_system",
        "named_tool",
        "standard_or_protocol",
        "named_work",
        "other_named_entity",
    }
)


def is_supported_entity_subtype(value: str | None) -> bool:
    """Return whether new analysis may emit this exact subtype."""
    return value in ENTITY_SUBTYPES
