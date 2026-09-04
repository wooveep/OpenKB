"""Closed Version Scope resolution and immutable query Navigation Snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Literal

from openkb.desktop_document_version_catalog import (
    DocumentLineage,
    DocumentVersionCatalogMember,
    DocumentVersionCatalogSnapshot,
    normalize_lineage_name,
)
from openkb.desktop_version_labels import normalize_version_label, version_label_candidates

VERSION_SCOPE_RESOLVER_VERSION = "openkb.version-scope.v1"
VersionMode = Literal["latest", "exact", "compare", "all", "unscoped"]
VersionScopeStatus = Literal["resolved", "ambiguous", "unavailable", "degraded"]
SupportingDocumentPolicy = Literal["selected_versions_only", "selected_versions_plus_independent"]


@dataclass(frozen=True)
class VersionFilter:
    mode: VersionMode | None = None
    lineage_ids: tuple[str, ...] = ()
    version_labels: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VersionScope:
    mode: VersionMode
    status: VersionScopeStatus
    lineage_ids: tuple[str, ...]
    requested_labels: tuple[str, ...]
    allowed_document_ids: frozenset[str]
    preferred_occurrence_document_ids: tuple[str, ...]
    supporting_document_policy: SupportingDocumentPolicy
    selection_reason: str
    catalog_source_revision: int
    catalog_generation_id: str
    degradation_reason: str | None = None
    available_labels: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["allowed_document_ids"] = sorted(self.allowed_document_ids)
        value["resolver_contract_version"] = VERSION_SCOPE_RESOLVER_VERSION
        return value


@dataclass(frozen=True)
class RetrievalRequest:
    question: str
    conversation_scope: VersionScope | None = None
    version_filter: VersionFilter | None = None
    requested_mode: VersionMode | None = None


@dataclass(frozen=True)
class NavigationSnapshot:
    snapshot_id: str
    version_scope: VersionScope
    version_catalog_revision_id: str
    version_catalog_digest: str
    active_knowledge_generation_id: int | None
    catalog_generation_id: str
    page_tree_generation_ids: tuple[str, ...]
    graph_result_ids: tuple[str, ...]
    page_tree_generation_bindings: tuple[tuple[str, str], ...] = ()
    graph_result_bindings: tuple[tuple[str, str], ...] = ()
    unusable_page_tree_document_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "version_scope": self.version_scope.as_dict(),
        }


def resolve_version_scope(
    question: str,
    *,
    conversation_scope: VersionScope | None,
    ui_filter: VersionFilter | None,
    catalog: DocumentVersionCatalogSnapshot,
) -> VersionScope:
    """Resolve version intent exactly once into a closed, immutable outcome."""
    available_members = tuple(
        (lineage, member)
        for lineage in catalog.lineages
        for member in lineage.members
        if member.availability == "available"
    )
    all_available = frozenset(member.document_id for _lineage, member in available_members)
    if ui_filter is not None and ui_filter.document_ids:
        selected = frozenset(ui_filter.document_ids) & all_available
        return _scope(
            mode=ui_filter.mode or "exact",
            status="resolved" if selected else "unavailable",
            lineages=ui_filter.lineage_ids,
            labels=ui_filter.version_labels,
            allowed=selected,
            preferred=tuple(value for value in ui_filter.document_ids if value in selected),
            reason="ui_document_filter",
            catalog=catalog,
        )
    normalized_question = normalize_lineage_name(question)
    mentioned_lineages = _mentioned_lineages(catalog.lineages, normalized_question)
    requested_lineages = frozenset(ui_filter.lineage_ids) if ui_filter is not None else frozenset()
    known_lineage_ids = frozenset(lineage.lineage_id for lineage in catalog.lineages)
    if requested_lineages - known_lineage_ids:
        return _scope(
            mode=(ui_filter.mode or "latest") if ui_filter is not None else "latest",
            status="unavailable",
            lineages=tuple(sorted(requested_lineages)),
            labels=ui_filter.version_labels if ui_filter is not None else (),
            allowed=frozenset(),
            preferred=(),
            reason="requested_lineage_unavailable",
            catalog=catalog,
        )
    selected_lineages = requested_lineages or mentioned_lineages
    known_labels = tuple(
        member.version_label
        for lineage in catalog.lineages
        if not selected_lineages or lineage.lineage_id in selected_lineages
        for member in lineage.members
        if member.version_label is not None and member.confirmed_at is not None
    )
    labels = _unique_version_labels(
        (
            *(ui_filter.version_labels if ui_filter is not None else ()),
            *version_label_candidates(question, known_labels=known_labels),
        )
    )
    if (
        not selected_lineages
        and labels
        and conversation_scope is not None
        and len(conversation_scope.lineage_ids) == 1
    ):
        selected_lineages = frozenset(conversation_scope.lineage_ids)
    explicit_mode = ui_filter.mode if ui_filter is not None and ui_filter.mode is not None else None
    mode = explicit_mode or _mode_from_question(question, labels)
    if mode == "all":
        return _scope(
            mode="all",
            status="resolved",
            lineages=tuple(sorted(selected_lineages)),
            labels=labels,
            allowed=frozenset(
                member.document_id
                for lineage, member in available_members
                if not selected_lineages or lineage.lineage_id in selected_lineages
            ),
            preferred=tuple(
                sorted(
                    member.document_id
                    for lineage, member in available_members
                    if not selected_lineages or lineage.lineage_id in selected_lineages
                )
            ),
            reason="explicit_all_versions",
            catalog=catalog,
        )
    if mode == "exact":
        return _resolve_labels(
            "exact",
            labels,
            selected_lineages,
            catalog,
        )
    if mode == "compare":
        return _resolve_labels(
            "compare",
            labels,
            selected_lineages,
            catalog,
        )
    if _asks_for_previous(question) and conversation_scope is not None:
        previous = _previous_scope(conversation_scope, catalog)
        if previous is not None:
            return previous
    return _latest_scope(catalog, selected_lineages)


def capture_navigation_snapshot_in(
    connection: sqlite3.Connection,
    scope: VersionScope,
    catalog: DocumentVersionCatalogSnapshot,
) -> NavigationSnapshot:
    """Pin all independently published navigation authorities for one retrieval."""
    knowledge = connection.execute(
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
    ).fetchone()
    catalog_row = connection.execute(
        "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
    ).fetchone()
    page_tree_rows = (
        connection.execute(
            "SELECT current.document_id, current.generation_id, generations.status "
            "FROM document_page_tree_current AS current "
            "LEFT JOIN document_page_tree_generations AS generations "
            "ON generations.generation_id = current.generation_id "
            "WHERE current.document_id IN ({}) ORDER BY current.document_id".format(
                _placeholders(scope.allowed_document_ids)
            ),
            tuple(sorted(scope.allowed_document_ids)),
        ).fetchall()
        if scope.allowed_document_ids
        else ()
    )
    page_tree_bindings = tuple(
        (str(row[0]), str(row[1])) for row in page_tree_rows if row[2] == "current"
    )
    unusable_page_tree_document_ids = tuple(
        str(row[0]) for row in page_tree_rows if row[2] != "current"
    )
    graph_result_bindings = (
        tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT document_id, result_id FROM knowledge_graph_current "
                "WHERE document_id IN ({}) ORDER BY document_id".format(
                    _placeholders(scope.allowed_document_ids)
                ),
                tuple(sorted(scope.allowed_document_ids)),
            ).fetchall()
        )
        if scope.allowed_document_ids
        else ()
    )
    page_trees = tuple(generation_id for _document_id, generation_id in page_tree_bindings)
    graph_results = tuple(result_id for _document_id, result_id in graph_result_bindings)
    payload = {
        "scope": scope.as_dict(),
        "version_catalog_revision_id": catalog.revision_id,
        "version_catalog_digest": catalog.snapshot_digest,
        "active_knowledge_generation_id": int(knowledge[0]) if knowledge else None,
        "catalog_generation_id": str(catalog_row[0]) if catalog_row and catalog_row[0] else "",
        "page_tree_generation_ids": page_trees,
        "unusable_page_tree_document_ids": unusable_page_tree_document_ids,
        "graph_result_ids": graph_results,
    }
    snapshot_id = (
        "navigation-"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    return NavigationSnapshot(
        snapshot_id=snapshot_id,
        version_scope=scope,
        version_catalog_revision_id=catalog.revision_id,
        version_catalog_digest=catalog.snapshot_digest,
        active_knowledge_generation_id=(int(knowledge[0]) if knowledge else None),
        catalog_generation_id=(
            str(catalog_row[0]) if catalog_row is not None and catalog_row[0] is not None else ""
        ),
        page_tree_generation_ids=page_trees,
        graph_result_ids=graph_results,
        page_tree_generation_bindings=page_tree_bindings,
        graph_result_bindings=graph_result_bindings,
        unusable_page_tree_document_ids=unusable_page_tree_document_ids,
    )


def _resolve_labels(
    mode: Literal["exact", "compare"],
    labels: tuple[str, ...],
    mentioned_lineages: frozenset[str],
    catalog: DocumentVersionCatalogSnapshot,
) -> VersionScope:
    expected = 2 if mode == "compare" else 1
    if len(labels) != expected:
        return _scope(
            mode=mode,
            status="ambiguous" if labels else "unavailable",
            lineages=tuple(sorted(mentioned_lineages)),
            labels=labels,
            allowed=frozenset(),
            preferred=(),
            reason="version_label_count_mismatch",
            catalog=catalog,
        )
    matches: list[tuple[DocumentLineage, DocumentVersionCatalogMember]] = []
    for label in labels:
        normalized = normalize_version_label(label)
        candidates = [
            (lineage, member)
            for lineage in catalog.lineages
            if lineage.lineage_state == "confirmed"
            and (not mentioned_lineages or lineage.lineage_id in mentioned_lineages)
            for member in lineage.members
            if member.confirmed_at is not None and member.normalized_version_label == normalized
        ]
        if len(candidates) != 1:
            available = tuple(
                sorted(
                    {
                        member.version_label
                        for lineage in catalog.lineages
                        if not mentioned_lineages or lineage.lineage_id in mentioned_lineages
                        for member in lineage.members
                        if member.availability == "available" and member.version_label is not None
                    }
                )
            )
            return _scope(
                mode=mode,
                status="ambiguous" if len(candidates) > 1 else "unavailable",
                lineages=tuple(sorted(mentioned_lineages)),
                labels=labels,
                allowed=frozenset(),
                preferred=(),
                reason="version_label_not_unique" if candidates else "exact_version_unavailable",
                catalog=catalog,
                available_labels=available,
            )
        matches.append(candidates[0])
    if mode == "compare" and matches[0][0].lineage_id != matches[1][0].lineage_id:
        return _scope(
            mode=mode,
            status="ambiguous",
            lineages=tuple(sorted({match[0].lineage_id for match in matches})),
            labels=labels,
            allowed=frozenset(),
            preferred=(),
            reason="compare_lineage_mismatch",
            catalog=catalog,
        )
    if any(member.availability != "available" for _lineage, member in matches):
        return _scope(
            mode=mode,
            status="unavailable",
            lineages=tuple(dict.fromkeys(lineage.lineage_id for lineage, _member in matches)),
            labels=labels,
            allowed=frozenset(),
            preferred=(),
            reason="exact_version_unavailable",
            catalog=catalog,
        )
    documents = tuple(member.document_id for _lineage, member in matches)
    return _scope(
        mode=mode,
        status="resolved",
        lineages=tuple(dict.fromkeys(lineage.lineage_id for lineage, _member in matches)),
        labels=labels,
        allowed=frozenset(documents),
        preferred=documents,
        reason="confirmed_version_labels",
        catalog=catalog,
    )


def _latest_scope(
    catalog: DocumentVersionCatalogSnapshot, mentioned: frozenset[str]
) -> VersionScope:
    allowed: set[str] = set()
    preferred: list[str] = []
    degraded = False
    unavailable = False
    selected_lineages: list[str] = []
    for lineage in catalog.lineages:
        if lineage.lineage_state != "confirmed":
            for member in lineage.members:
                if member.availability == "available":
                    allowed.add(member.document_id)
            continue
        if mentioned and lineage.lineage_id not in mentioned:
            continue
        selected_lineages.append(lineage.lineage_id)
        current = _member(lineage, lineage.current_document_id)
        if current is not None and current.availability == "available":
            allowed.add(current.document_id)
            preferred.append(current.document_id)
            continue
        fallback = _nearest_available_predecessor(lineage, current)
        if fallback is None:
            unavailable = True
            continue
        degraded = True
        allowed.add(fallback.document_id)
        preferred.append(fallback.document_id)
    status: VersionScopeStatus = (
        "degraded" if degraded else ("unavailable" if unavailable and not allowed else "resolved")
    )
    return _scope(
        mode="latest",
        status=status,
        lineages=tuple(selected_lineages),
        labels=(),
        allowed=frozenset(allowed),
        preferred=tuple(preferred),
        reason="confirmed_current_versions_plus_independent",
        catalog=catalog,
        degradation_reason=("current_unavailable_confirmed_predecessor" if degraded else None),
    )


def _previous_scope(
    previous: VersionScope, catalog: DocumentVersionCatalogSnapshot
) -> VersionScope | None:
    if len(previous.lineage_ids) != 1 or not previous.preferred_occurrence_document_ids:
        return None
    lineage = next(
        (value for value in catalog.lineages if value.lineage_id == previous.lineage_ids[0]),
        None,
    )
    if lineage is None:
        return None
    selected = _member(lineage, previous.preferred_occurrence_document_ids[0])
    predecessor = _member(lineage, selected.predecessor_document_id if selected else None)
    if predecessor is None or predecessor.availability != "available":
        return _scope(
            mode="exact",
            status="unavailable",
            lineages=(lineage.lineage_id,),
            labels=(),
            allowed=frozenset(),
            preferred=(),
            reason="previous_version_unavailable",
            catalog=catalog,
        )
    return _scope(
        mode="exact",
        status="resolved",
        lineages=(lineage.lineage_id,),
        labels=((predecessor.version_label,) if predecessor.version_label else ()),
        allowed=frozenset((predecessor.document_id,)),
        preferred=(predecessor.document_id,),
        reason="conversation_previous_version",
        catalog=catalog,
    )


def _scope(
    *,
    mode: VersionMode,
    status: VersionScopeStatus,
    lineages: tuple[str, ...],
    labels: tuple[str, ...],
    allowed: frozenset[str],
    preferred: tuple[str, ...],
    reason: str,
    catalog: DocumentVersionCatalogSnapshot,
    degradation_reason: str | None = None,
    available_labels: tuple[str, ...] = (),
) -> VersionScope:
    return VersionScope(
        mode=mode,
        status=status,
        lineage_ids=lineages,
        requested_labels=labels,
        allowed_document_ids=allowed,
        preferred_occurrence_document_ids=preferred,
        supporting_document_policy=(
            "selected_versions_plus_independent"
            if mode in {"latest", "unscoped"}
            else "selected_versions_only"
        ),
        selection_reason=reason,
        catalog_source_revision=catalog.source_revision,
        catalog_generation_id=catalog.revision_id,
        degradation_reason=degradation_reason,
        available_labels=available_labels,
    )


def _mentioned_lineages(
    lineages: tuple[DocumentLineage, ...], normalized_question: str
) -> frozenset[str]:
    matches = []
    for lineage in lineages:
        names = (lineage.display_name, *lineage.aliases)
        if any(
            (normalized := normalize_lineage_name(name)) and normalized in normalized_question
            for name in names
        ):
            matches.append(lineage.lineage_id)
    return frozenset(matches)


def _mode_from_question(question: str, labels: tuple[str, ...]) -> VersionMode:
    lowered = normalize_lineage_name(question)
    if any(value in lowered for value in ("所有版本", "全部版本", "all versions")):
        return "all"
    if len(labels) >= 2 or any(
        value in lowered for value in ("比较", "对比", " vs ", " versus ", "difference")
    ):
        return "compare"
    if labels:
        return "exact"
    return "latest"


def _asks_for_previous(question: str) -> bool:
    lowered = normalize_lineage_name(question)
    return any(value in lowered for value in ("上一版", "前一版", "previous version"))


def _unique_version_labels(values: tuple[str, ...]) -> tuple[str, ...]:
    labels: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_version_label(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        labels.append(value)
    return tuple(labels)


def _member(
    lineage: DocumentLineage, document_id: str | None
) -> DocumentVersionCatalogMember | None:
    return next((member for member in lineage.members if member.document_id == document_id), None)


def _nearest_available_predecessor(
    lineage: DocumentLineage, start: DocumentVersionCatalogMember | None
) -> DocumentVersionCatalogMember | None:
    seen: set[str] = set()
    current = start
    while current is not None and current.predecessor_document_id is not None:
        if current.document_id in seen:
            return None
        seen.add(current.document_id)
        current = _member(lineage, current.predecessor_document_id)
        if current is not None and current.availability == "available":
            return current
    return None


def _placeholders(values: frozenset[str]) -> str:
    return ", ".join("?" for _ in values)
