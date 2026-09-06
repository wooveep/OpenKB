"""Strict local validation primitives for model-proposed navigation decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass

from openkb.retrieval.trace import DesktopFacetCoverageTrace

_SQL_TERM = re.compile(
    r"^\s*(?:pragma\b|select\s+(?:\*|\d+|['\"])|select\b.+\b(?:from|where|join)\b)",
    re.IGNORECASE,
)


def require_exact_object_fields(value: object, expected: set[str], *, context: str) -> None:
    """Reject shape drift while returning a useful evidence-safe repair message."""
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object.")
    actual = set(value)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise ValueError(
            f"{context} fields are invalid: missing={missing}, unexpected={unexpected}."
        )


def bounded_string_array(value: object, *, maximum: int, item_limit: int) -> tuple[str, ...]:
    """Normalize a bounded unique JSON string array."""
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Navigation string array is invalid.")
    items = tuple(bounded_string(item, item_limit) for item in value)
    if len(items) != len(set(item.casefold() for item in items)):
        raise ValueError("Navigation string array contains duplicates.")
    return items


def bounded_string(value: object, maximum: int) -> str:
    """Normalize one non-empty bounded JSON string."""
    if not isinstance(value, str):
        raise ValueError("Navigation string is invalid.")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError("Navigation string is empty or too long.")
    return normalized


def unsafe_navigation_term(term: str) -> bool:
    """Reject path, URI, traversal and SQL-shaped search terms."""
    normalized = term.casefold()
    return (
        len(term) > 120
        or term.startswith(("/", "\\"))
        or ":\\" in term
        or "file://" in normalized
        or _SQL_TERM.search(normalized) is not None
        or "../" in term
        or "..\\" in term
    )


@dataclass(frozen=True)
class NavigationAction:
    """One validated query-scoped expansion request bound to an open required facet."""

    kind: str
    facet_id: str
    terms: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        values = (*self.terms, *self.routes, *self.evidence_ids)
        normalized = tuple(sorted(value.casefold() for value in values))
        return f"{self.kind}:{'|'.join(normalized)}"


def validated_navigation_actions(
    value: object,
    *,
    visited_action_ids: frozenset[str],
    available_routes: frozenset[str],
    completed_routes: frozenset[str] = frozenset(),
    known_evidence_ids: frozenset[str],
    coverage: tuple[DesktopFacetCoverageTrace, ...],
    required_facet_ids: frozenset[str],
    maximum_actions: int,
) -> tuple[NavigationAction, ...]:
    """Validate a bounded batch of explicitly facet-bound model actions."""
    if not isinstance(value, list) or len(value) > maximum_actions:
        raise ValueError("Navigation action batch exceeds its remaining budget.")
    actions: list[NavigationAction] = []
    seen: set[str] = set()
    open_facets = tuple(
        item.facet_id
        for item in coverage
        if item.facet_id in required_facet_ids and item.state in {"missing", "partial"}
    )
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Navigation action is invalid.")
        if not open_facets:
            raise ValueError("Navigation actions require an uncovered required facet.")
        kind = item.get("kind")
        if not isinstance(kind, str):
            raise ValueError("Navigation action kind is not allowed.")
        action_fields = {
            "search_routes": {"kind", "terms"},
            "read_routes": {"kind", "routes"},
            "read_source_sections": {"kind", "evidence_ids"},
        }.get(kind)
        if action_fields is None:
            raise ValueError("Navigation action kind is not allowed.")
        _require_action_fields(item, action_fields)
        facet_id = bounded_string(item.get("facet_id"), 160)
        if facet_id not in open_facets:
            raise ValueError("Navigation action facet is not currently open and required.")
        if kind == "search_routes":
            terms = bounded_string_array(item["terms"], maximum=8, item_limit=120)
            if not terms or any(unsafe_navigation_term(term) for term in terms):
                raise ValueError("Route search terms are missing or unsafe.")
            action = NavigationAction("search_routes", facet_id, terms=terms)
        elif kind == "read_routes":
            routes = bounded_string_array(item["routes"], maximum=4, item_limit=320)
            unread_routes = tuple(route for route in routes if route not in completed_routes)
            if not unread_routes:
                continue
            if not set(unread_routes) <= available_routes:
                raise ValueError("Navigation route is unavailable or unpublished.")
            action = NavigationAction("read_routes", facet_id, routes=unread_routes)
        elif kind == "read_source_sections":
            evidence_ids = bounded_string_array(item["evidence_ids"], maximum=4, item_limit=160)
            if not evidence_ids or not set(evidence_ids) <= known_evidence_ids:
                raise ValueError("Source section anchor is not known Available Evidence.")
            action = NavigationAction("read_source_sections", facet_id, evidence_ids=evidence_ids)
        if action.identity in seen or action.identity in visited_action_ids:
            # A repeated read cannot add Evidence. Discard it locally so one
            # redundant aspect binding does not invalidate an otherwise safe
            # decision or spend the bounded structured-repair call.
            continue
        seen.add(action.identity)
        actions.append(action)
    return tuple(actions)


def _require_action_fields(item: dict[object, object], fields: set[str]) -> None:
    """Require the production contract, including an explicit open facet."""
    actual = frozenset(item)
    if actual != frozenset((*fields, "facet_id")):
        raise ValueError("Navigation action must contain exactly its allowed fields.")
