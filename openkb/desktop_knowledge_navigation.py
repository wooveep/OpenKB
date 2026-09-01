"""Bounded virtual Knowledge Navigation over one pinned SQLite snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from openkb.desktop_answer_types import (
    DesktopEvidenceRef,
    DesktopKnowledgeGuidance,
    DesktopKnowledgeRouteOption,
)
from openkb.desktop_knowledge_inventory import eligible_knowledge_routes_in
from openkb.desktop_knowledge_navigation_routes import (
    _catalog_descriptors_in,
    _index_descriptors,
    _inventory_descriptor,
    _phase_diverse_route_descriptors,
    _ReadDescriptor,
    _select_read_descriptors,
    _summary_descriptors_in,
    _unique_preserving_descriptors,
    _unique_ranked_descriptors,
)
from openkb.desktop_knowledge_navigation_windows import (
    consolidate_seed_source_windows as _consolidate_seed_source_windows,
)
from openkb.desktop_knowledge_navigation_windows import (
    phase_diverse_source_window as _phase_diverse_source_window,
)
from openkb.desktop_knowledge_navigation_windows import (
    round_robin_source_windows as _round_robin_source_windows,
)
from openkb.desktop_navigation_ranking import (
    broad_source_outline_anchor_in as _broad_source_outline_anchor_in,
)
from openkb.desktop_navigation_ranking import source_relevance_in as _source_relevance_in
from openkb.desktop_navigation_ranking import (
    unrequested_scope_penalty as _unrequested_scope_penalty,
)
from openkb.desktop_source_sections import (
    is_administrative_section as _is_administrative_section,
)
from openkb.desktop_source_sections import (
    section_from_heading_path as _section,
)
from openkb.desktop_source_sections import (
    source_section_evidence_in,
)
from openkb.desktop_source_sections import (
    term_match_count as _term_match_count,
)

NAVIGATION_MAX_READS = 8
NAVIGATION_MAX_SOURCE_WINDOWS = 4
NAVIGATION_REPEAT_ROUTE_PENALTY = 4
NAVIGATION_MAX_STRUCTURAL_ANCHORS = 2

_KNOWLEDGE_KINDS = ("concept", "entity", "procedure")
_PROCEDURE_QUERY_MARKERS = (
    "build",
    "configure",
    "deploy",
    "how to",
    "install",
    "migrate",
    "procedure",
    "setup",
    "step",
    "安装",
    "搭建",
    "步骤",
    "流程",
    "迁移",
    "配置",
    "部署",
    "如何",
)


@dataclass(frozen=True)
class _GuidanceUnit:
    text: str
    evidence_ids: tuple[str, ...]
    role: str | None = None


@dataclass(frozen=True)
class _NavigationRead:
    route: str
    kind: str
    authority: str
    title: str
    units: tuple[_GuidanceUnit, ...]
    hop: int
    snapshot_token: str


@dataclass(frozen=True)
class DesktopKnowledgeNavigationResult:
    """Selected virtual reads and original-source supplements for one query."""

    snapshot_id: str | None = None
    reads: tuple[_NavigationRead, ...] = ()
    source_windows: tuple[DesktopEvidenceRef, ...] = ()
    source_read_count: int = 0
    route_options: tuple[DesktopKnowledgeRouteOption, ...] = ()
    degradation_reasons: tuple[str, ...] = ()

    @property
    def read_count(self) -> int:
        return len(self.reads)

    @property
    def source_window_count(self) -> int:
        return self.source_read_count or len(self.source_windows)

    @property
    def link_hop_count(self) -> int:
        return max((read.hop for read in self.reads), default=0)

    @property
    def routes(self) -> tuple[str, ...]:
        return tuple(read.route for read in self.reads)

    def grounded_guidance(
        self,
        evidence_ids: tuple[str, ...],
        *,
        page_tree_supplemented: bool,
    ) -> tuple[tuple[DesktopKnowledgeGuidance, ...], str]:
        """Expose only factual units whose declared Evidence reached the answer pack."""
        available = frozenset(evidence_ids)
        guidance: list[DesktopKnowledgeGuidance] = []
        total_units = 0
        grounded_units = 0
        for read in self.reads:
            total_units += len(read.units)
            units = tuple(
                unit
                for unit in read.units
                if unit.evidence_ids and set(unit.evidence_ids) <= available
            )
            grounded_units += len(units)
            if not units:
                continue
            source_ids = tuple(
                dict.fromkeys(evidence_id for unit in units for evidence_id in unit.evidence_ids)
            )
            guidance.append(
                DesktopKnowledgeGuidance(
                    route=read.route,
                    kind=read.kind,
                    authority=read.authority,
                    title=read.title,
                    content_markdown="\n".join(f"- {unit.text}" for unit in units),
                    source_evidence_ids=source_ids,
                )
            )
        if total_units == 0:
            state = "not_applicable"
        elif grounded_units == total_units:
            state = "supplemented" if self.source_windows or page_tree_supplemented else "covered"
        elif grounded_units:
            state = "partial"
        else:
            state = "uncovered"
        return tuple(guidance), state


def build_knowledge_navigation_in(
    connection: sqlite3.Connection,
    *,
    catalog_generation_id: str | None,
    terms: tuple[str, ...],
    baseline_evidence: tuple[DesktopEvidenceRef, ...],
    max_reads: int = NAVIGATION_MAX_READS,
    max_source_windows: int = NAVIGATION_MAX_SOURCE_WINDOWS,
    excluded_routes: frozenset[str] = frozenset(),
    requested_routes: tuple[str, ...] = (),
    requested_evidence_ids: tuple[str, ...] = (),
) -> DesktopKnowledgeNavigationResult:
    """Spend the supplied slice of the session-wide logical-read budget."""
    if catalog_generation_id is None:
        return DesktopKnowledgeNavigationResult(
            degradation_reasons=("knowledge_navigation_snapshot_unavailable",)
        )
    normalized_terms = tuple(
        dict.fromkeys(term.strip().casefold() for term in terms if term.strip())
    )
    inventory = eligible_knowledge_routes_in(connection)
    matching_inventory = _phase_diverse_route_descriptors(
        _unique_ranked_descriptors(
            tuple(
                _inventory_descriptor(item, normalized_terms)
                for item in inventory
                if any(term in item.title.casefold() for term in normalized_terms)
            )
        )
    )
    summary_descriptors = _summary_descriptors_in(
        connection,
        normalized_terms,
        baseline_evidence,
        inventory,
    )
    summary_document_ids = {item.authority_id for item in summary_descriptors}
    source_outlines = tuple(
        _inventory_descriptor(item, normalized_terms)
        for item in inventory
        if item.authority == "source_document" and item.identity in summary_document_ids
    )
    matched_descriptors = _unique_ranked_descriptors(
        (
            *_catalog_descriptors_in(
                connection,
                catalog_generation_id,
                normalized_terms,
                inventory,
            ),
            *matching_inventory,
            *summary_descriptors,
            *source_outlines,
        )
    )
    index_descriptors = _index_descriptors(inventory)
    requested_inventory = tuple(
        _inventory_descriptor(item, normalized_terms)
        for item in inventory
        if item.route in requested_routes
    )
    descriptors = _unique_ranked_descriptors(
        (
            *matched_descriptors,
            *requested_inventory,
            *(item for item in index_descriptors if item.route in requested_routes),
        )
    )
    requested_index_kinds = {
        item.authority_id for item in index_descriptors if item.route in requested_routes
    }
    root_index_requested = "root" in requested_index_kinds
    expanded_inventory = tuple(
        _inventory_descriptor(item, normalized_terms)
        for item in inventory
        if item.kind in requested_index_kinds
    )
    advertised = _unique_preserving_descriptors(
        (
            *_phase_diverse_route_descriptors(
                _unique_ranked_descriptors(
                    tuple(_inventory_descriptor(item, normalized_terms) for item in inventory)
                )
            ),
            *index_descriptors,
        )
        if root_index_requested
        else _prioritized_advertised_descriptors(
            matched_descriptors=matched_descriptors,
            matching_inventory=matching_inventory,
            summary_descriptors=summary_descriptors,
            source_outlines=source_outlines,
            expanded_inventory=expanded_inventory,
            requested_inventory=requested_inventory,
            index_descriptors=index_descriptors,
        )
    )
    route_options = tuple(
        DesktopKnowledgeRouteOption(item.route, item.kind, item.title) for item in advertised
    )
    selected = _select_read_descriptors(
        descriptors,
        max_reads=max(0, max_reads),
        excluded_routes=excluded_routes,
        requested_routes=requested_routes,
        requested_only=bool(requested_routes or requested_evidence_ids),
    )
    reads = tuple(
        read
        for descriptor in selected
        if (read := _resolve_read_in(connection, descriptor)) is not None and read.units
    )
    baseline_ids = {reference.evidence_id for reference in baseline_evidence}
    automatic_source_expansion = _automatic_source_expansion(normalized_terms, reads)
    structural_anchors = (
        _structural_anchor_evidence_ids(baseline_evidence, terms=normalized_terms)
        if automatic_source_expansion
        and not excluded_routes
        and not requested_routes
        and not requested_evidence_ids
        else ()
    )
    routed_source_ids = (
        _rank_source_evidence_ids_in(
            connection,
            reads,
            terms=normalized_terms,
            baseline_ids=frozenset(baseline_ids),
        )
        if automatic_source_expansion or requested_routes
        else ()
    )
    missing_ids = tuple(
        dict.fromkeys(
            (
                *requested_evidence_ids,
                *routed_source_ids,
                *structural_anchors,
            )
        )
    )
    source_windows: list[tuple[DesktopEvidenceRef, ...]] = []
    source_read_count = 0
    for evidence_id in missing_ids:
        if source_read_count == max(0, max_source_windows):
            break
        section_evidence = source_section_evidence_in(
            connection,
            evidence_id,
            terms=normalized_terms,
        )
        if section_evidence:
            source_read_count += 1
            source_windows.append(
                _phase_diverse_source_window(section_evidence, terms=normalized_terms)
            )
    ordered_windows = tuple(source_windows)
    if not requested_routes and not requested_evidence_ids:
        ordered_windows = _consolidate_seed_source_windows(ordered_windows)
    windows = _round_robin_source_windows(ordered_windows)
    snapshot_material = "\n".join(
        (catalog_generation_id, *(f"{read.route}:{read.snapshot_token}" for read in reads))
    )
    snapshot_id = f"navigation-{hashlib.sha256(snapshot_material.encode()).hexdigest()[:24]}"
    return DesktopKnowledgeNavigationResult(
        snapshot_id=snapshot_id,
        reads=reads,
        source_windows=windows,
        source_read_count=source_read_count,
        route_options=route_options,
    )


def _automatic_source_expansion(
    terms: tuple[str, ...],
    reads: tuple[_NavigationRead, ...],
) -> bool:
    """Reserve broad source reads for procedural routes or action-shaped queries."""
    return any(read.kind == "procedure" for read in reads) or any(
        marker in term for term in terms for marker in _PROCEDURE_QUERY_MARKERS
    )


def _resolve_read_in(
    connection: sqlite3.Connection, descriptor: _ReadDescriptor
) -> _NavigationRead | None:
    units: tuple[_GuidanceUnit, ...]
    if descriptor.descriptor_kind == "index":
        units = (
            _GuidanceUnit(
                f"Browse the current {descriptor.title.casefold()} routes.",
                (),
                "route_index",
            ),
        )
    elif descriptor.descriptor_kind == "summary":
        units = _summary_units_in(connection, descriptor.authority_id)
        if not units:
            units = _source_structure_units_in(connection, descriptor.authority_id)
    elif descriptor.authority == "source_section":
        units = _source_section_units_in(connection, descriptor)
    elif descriptor.descriptor_kind == "source":
        units = _source_structure_units_in(connection, descriptor.authority_id)
    elif descriptor.authority == "published_generation":
        units = _generated_units_in(connection, descriptor)
    elif descriptor.authority == "user_revision":
        units = _user_units_in(connection, descriptor)
    else:
        return None
    if not units:
        return None
    return _NavigationRead(
        route=descriptor.route,
        kind=descriptor.kind,
        authority=descriptor.authority,
        title=descriptor.title,
        units=units,
        hop=descriptor.hop,
        snapshot_token=descriptor.snapshot_token,
    )


def _summary_units_in(
    connection: sqlite3.Connection, document_id: str
) -> tuple[_GuidanceUnit, ...]:
    rows = connection.execute(
        """
        SELECT units.unit_ordinal, units.unit_text, units.role, sources.evidence_id
        FROM document_summary_units AS units
        JOIN document_summary_unit_sources AS sources
          ON sources.document_id = units.document_id
         AND sources.unit_ordinal = units.unit_ordinal
        WHERE units.document_id = ?
        ORDER BY units.unit_ordinal, sources.evidence_id
        """,
        (document_id,),
    ).fetchall()
    return _group_units(
        rows,
        ordinal_index=0,
        text_index=1,
        evidence_index=3,
        role_index=2,
    )


def _source_structure_units_in(
    connection: sqlite3.Connection, document_id: str
) -> tuple[_GuidanceUnit, ...]:
    rows = connection.execute(
        """
        SELECT blocks.heading_path, occurrences.evidence_id
        FROM document_ir_blocks AS blocks
        LEFT JOIN evidence_occurrences AS occurrences
          ON occurrences.document_id = blocks.document_id
         AND occurrences.block_id = blocks.block_id
        WHERE blocks.document_id = ?
        ORDER BY blocks.ordinal, occurrences.ordinal
        """,
        (document_id,),
    ).fetchall()
    units: list[_GuidanceUnit] = []
    seen: set[str] = set()
    fallback_evidence_id: str | None = None
    for heading_value, evidence_value in rows:
        if evidence_value is not None and fallback_evidence_id is None:
            fallback_evidence_id = str(evidence_value)
        heading = _section(str(heading_value))
        key = heading.casefold()
        if not heading or key in seen or evidence_value is None:
            continue
        seen.add(key)
        units.append(_GuidanceUnit(heading, (str(evidence_value),), "section"))
    if units:
        return tuple(units)
    if fallback_evidence_id is None:
        return ()
    return (_GuidanceUnit("Document source", (fallback_evidence_id,), "source"),)


def _source_section_units_in(
    connection: sqlite3.Connection, descriptor: _ReadDescriptor
) -> tuple[_GuidanceUnit, ...]:
    metadata = _json_object(descriptor.metadata_json)
    document_id = metadata.get("document_id")
    heading_path_json = metadata.get("heading_path_json")
    if not isinstance(document_id, str) or not isinstance(heading_path_json, str):
        return ()
    rows = connection.execute(
        """
        SELECT occurrences.evidence_id
        FROM document_ir_blocks AS blocks
        JOIN evidence_occurrences AS occurrences
          ON occurrences.document_id = blocks.document_id
         AND occurrences.block_id = blocks.block_id
        JOIN source_documents AS documents ON documents.document_id = blocks.document_id
        WHERE blocks.document_id = ? AND blocks.heading_path = ?
          AND documents.availability = 'available'
        ORDER BY blocks.ordinal, occurrences.ordinal, occurrences.evidence_id
        """,
        (document_id, heading_path_json),
    ).fetchall()
    evidence_ids = tuple(dict.fromkeys(str(row[0]) for row in rows))
    if not evidence_ids:
        return ()
    return (_GuidanceUnit(descriptor.title, evidence_ids, "section"),)


def _generated_units_in(
    connection: sqlite3.Connection, descriptor: _ReadDescriptor
) -> tuple[_GuidanceUnit, ...]:
    metadata = _json_object(descriptor.metadata_json)
    generation_id = metadata.get("generation_id")
    if not isinstance(generation_id, int):
        return ()
    valid = connection.execute(
        """
        SELECT 1
        FROM knowledge_generation_items AS items
        JOIN knowledge_generations AS generations
            ON generations.generation_id = items.generation_id
        WHERE items.generation_id = ? AND items.item_key = ?
            AND items.kind = ? AND generations.qualification_state = 'qualified'
        """,
        (generation_id, descriptor.authority_id, descriptor.kind),
    ).fetchone()
    if valid is None:
        return ()
    rows = connection.execute(
        """
        SELECT sources.claim_text, sources.evidence_id
        FROM knowledge_generation_item_sources AS sources
        WHERE sources.generation_id = ? AND sources.item_key = ?
          AND EXISTS (
              SELECT 1 FROM evidence_occurrences AS occurrences
              JOIN source_documents AS documents
                ON documents.document_id = occurrences.document_id
              WHERE occurrences.evidence_id = sources.evidence_id
                AND documents.availability = 'available'
          )
        ORDER BY sources.source_id, sources.evidence_id
        """,
        (generation_id, descriptor.authority_id),
    ).fetchall()
    return _claim_units(rows)


def _user_units_in(
    connection: sqlite3.Connection, descriptor: _ReadDescriptor
) -> tuple[_GuidanceUnit, ...]:
    metadata = _json_object(descriptor.metadata_json)
    revision_id = metadata.get("revision_id")
    if not isinstance(revision_id, str) or not revision_id:
        return ()
    valid = connection.execute(
        """
        SELECT 1 FROM knowledge_page_revisions
        WHERE revision_id = ? AND page_id = ? AND provenance_state = 'source_backed'
        """,
        (revision_id, descriptor.authority_id),
    ).fetchone()
    if valid is None:
        return ()
    rows = connection.execute(
        """
        SELECT sources.claim_text, sources.evidence_id
        FROM knowledge_page_revision_sources AS sources
        WHERE sources.revision_id = ?
          AND EXISTS (
              SELECT 1 FROM evidence_occurrences AS occurrences
              JOIN source_documents AS documents
                ON documents.document_id = occurrences.document_id
              WHERE occurrences.evidence_id = sources.evidence_id
                AND documents.availability = 'available'
          )
        ORDER BY sources.source_id, sources.evidence_id
        """,
        (revision_id,),
    ).fetchall()
    return _claim_units(rows)


def _claim_units(rows: list[tuple[object, ...]]) -> tuple[_GuidanceUnit, ...]:
    evidence_by_claim: defaultdict[str, set[str]] = defaultdict(set)
    display_by_claim: dict[str, str] = {}
    for claim_value, evidence_value in rows:
        for claim in str(claim_value).splitlines():
            text = " ".join(claim.split())
            if not text:
                continue
            key = text.casefold()
            display_by_claim.setdefault(key, text)
            evidence_by_claim[key].add(str(evidence_value))
    return tuple(
        _GuidanceUnit(display_by_claim[key], tuple(sorted(evidence_by_claim[key])))
        for key in display_by_claim
    )


def _group_units(
    rows: list[tuple[object, ...]],
    *,
    ordinal_index: int,
    text_index: int,
    evidence_index: int,
    role_index: int | None = None,
) -> tuple[_GuidanceUnit, ...]:
    grouped: defaultdict[int, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        grouped[int(str(row[ordinal_index]))].append(row)
    return tuple(
        _GuidanceUnit(
            str(values[0][text_index]),
            tuple(dict.fromkeys(str(value[evidence_index]) for value in values)),
            str(values[0][role_index]) if role_index is not None else None,
        )
        for _ordinal, values in sorted(grouped.items())
    )


def _rank_source_evidence_ids_in(
    connection: sqlite3.Connection,
    reads: tuple[_NavigationRead, ...],
    *,
    terms: tuple[str, ...],
    baseline_ids: frozenset[str],
) -> tuple[str, ...]:
    """Prefer new routes over similarly relevant extra units without admitting noise."""
    ranked_candidates: list[tuple[tuple[int, int, int, int, str], str, tuple[str, ...]]] = []
    source_outline_anchors: list[str] = []
    for read_ordinal, read in enumerate(reads):
        read_candidates: list[tuple[tuple[int, int, int, int, str], str, tuple[str, ...]]] = []
        for unit_ordinal, unit in enumerate(read.units):
            unit_matches = _term_match_count(unit.text, terms)
            for evidence_ordinal, evidence_id in enumerate(unit.evidence_ids):
                relevance = _source_relevance_in(connection, evidence_id, terms)
                if relevance is None:
                    continue
                section_matches, administrative, section, document_id = relevance
                # The original section is the routing authority. Summary prose can be broad
                # or multilingual, so it may refine but must not outweigh its own source.
                effective_score = (
                    section_matches * 2 + unit_matches + _guidance_role_bonus(unit.role)
                )
                effective_score -= _unrequested_scope_penalty(
                    section,
                    terms,
                )
                if evidence_id in baseline_ids:
                    # The baseline pack is bounded again after routed fusion. Keep an
                    # existing anchor eligible for section expansion, but prefer a
                    # genuinely new anchor when both are equally relevant.
                    effective_score -= 1
                if administrative:
                    effective_score -= 8
                key = (
                    -effective_score,
                    read_ordinal,
                    unit_ordinal,
                    evidence_ordinal,
                    evidence_id,
                )
                phase = (document_id, *_source_anchor_phase_key(section))
                read_candidates.append((key, evidence_id, phase))
        if read_candidates:
            read_candidates.sort(key=lambda item: item[0])
            seen_evidence: set[str] = set()
            unique_candidates: list[
                tuple[tuple[int, int, int, int, str], str, tuple[str, ...]]
            ] = []
            for item in read_candidates:
                if item[1] in seen_evidence:
                    continue
                seen_evidence.add(item[1])
                unique_candidates.append(item)
            if read.authority == "source_document":
                outline_anchor = _broad_source_outline_anchor_in(
                    connection,
                    tuple(item[1] for item in unique_candidates),
                    terms,
                )
                if outline_anchor is not None:
                    source_outline_anchors.append(outline_anchor)
            for route_position, (key, evidence_id, phase) in enumerate(unique_candidates):
                repeat_penalty = min(route_position, 3) * NAVIGATION_REPEAT_ROUTE_PENALTY
                ranked_candidates.append(((key[0] + repeat_penalty, *key[1:]), evidence_id, phase))
    ranked_candidates.sort(key=lambda item: item[0])
    phase_representatives: list[tuple[tuple[int, int, int, int, str], str, tuple[str, ...]]] = []
    deferred: list[tuple[tuple[int, int, int, int, str], str, tuple[str, ...]]] = []
    seen_phases: set[tuple[str, ...]] = set()
    for candidate in ranked_candidates:
        if candidate[2] in seen_phases:
            deferred.append(candidate)
            continue
        seen_phases.add(candidate[2])
        phase_representatives.append(candidate)
    return tuple(
        dict.fromkeys(
            (
                *source_outline_anchors,
                *(item[1] for item in (*phase_representatives, *deferred)),
            )
        )
    )


def _source_anchor_phase_key(section: str) -> tuple[str, ...]:
    """Group adjacent detail anchors under their document-level major phase."""
    parts = tuple(
        " ".join(part.split()).casefold() for part in section.split(" / ") if " ".join(part.split())
    )
    return parts[:2] or ("",)


def _prioritized_advertised_descriptors(
    *,
    matched_descriptors: tuple[_ReadDescriptor, ...],
    matching_inventory: tuple[_ReadDescriptor, ...],
    summary_descriptors: tuple[_ReadDescriptor, ...],
    source_outlines: tuple[_ReadDescriptor, ...],
    expanded_inventory: tuple[_ReadDescriptor, ...],
    requested_inventory: tuple[_ReadDescriptor, ...],
    index_descriptors: tuple[_ReadDescriptor, ...],
) -> tuple[_ReadDescriptor, ...]:
    """Put an original-like navigation ladder inside the 24-route prompt window."""
    generated = tuple(
        item
        for item in matched_descriptors
        if item.authority
        not in {"source_section", "source_document", "document_summary", "navigation_index"}
    )
    source_sections = tuple(
        item for item in matching_inventory if item.authority == "source_section"
    )
    priority = _unique_preserving_descriptors(
        (
            *generated[:4],
            *summary_descriptors[:2],
            *source_outlines[:2],
            *source_sections[:10],
            *index_descriptors[:6],
        )
    )
    return _unique_preserving_descriptors(
        (
            *priority,
            *matching_inventory,
            *expanded_inventory,
            *matched_descriptors,
            *requested_inventory,
            *index_descriptors,
        )
    )


def _structural_anchor_evidence_ids(
    evidence: tuple[DesktopEvidenceRef, ...],
    *,
    terms: tuple[str, ...],
) -> tuple[str, ...]:
    """Reserve the strongest matching PageTree anchor from each relevant document."""
    by_document: dict[str, tuple[tuple[int, int, int, str], str]] = {}
    seen_sections: set[tuple[str, str]] = set()
    for ordinal, reference in enumerate(evidence):
        normalized_section = " ".join(reference.section.split())
        section_key = (reference.document_id, normalized_section.casefold())
        if section_key in seen_sections or _is_administrative_section(normalized_section):
            continue
        seen_sections.add(section_key)
        leaf = normalized_section.rsplit(" / ", 1)[-1]
        excerpt = " ".join(reference.excerpt.split())
        if excerpt.casefold() != leaf.casefold() and "document_page_tree" not in reference.channels:
            continue
        matches = _term_match_count(normalized_section, terms)
        if matches <= 0:
            continue
        depth = normalized_section.count(" / ")
        candidate = (
            (-matches, depth, ordinal, reference.evidence_id),
            reference.evidence_id,
        )
        existing = by_document.get(reference.document_id)
        if existing is None or candidate[0] < existing[0]:
            by_document[reference.document_id] = candidate
    candidates = sorted(by_document.values(), key=lambda item: item[0])
    return tuple(item[1] for item in candidates[:NAVIGATION_MAX_STRUCTURAL_ANCHORS])


def _guidance_role_bonus(role: str | None) -> int:
    return {"key_topic": 3, "purpose": 1}.get(role or "", 0)


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}
