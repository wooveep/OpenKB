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
from openkb.desktop_knowledge_routes import knowledge_route, summary_route
from openkb.desktop_source_sections import (
    is_administrative_section as _is_administrative_section,
)
from openkb.desktop_source_sections import (
    section_from_heading_path as _section,
)
from openkb.desktop_source_sections import (
    source_occurrence_sort_key as _source_occurrence_sort_key,
)
from openkb.desktop_source_sections import (
    source_occurrences_in as _source_occurrences_in,
)
from openkb.desktop_source_sections import (
    source_section_evidence_in,
)
from openkb.desktop_source_sections import (
    term_match_count as _term_match_count,
)

NAVIGATION_MAX_READS = 8
NAVIGATION_MAX_SOURCE_WINDOWS = 4
NAVIGATION_MAX_LINK_HOPS = 1

_KNOWLEDGE_KINDS = ("concept", "entity", "procedure")


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
class _ReadDescriptor:
    score: int
    hop: int
    descriptor_kind: str
    authority: str
    authority_id: str
    kind: str
    title: str
    metadata_json: str
    route: str
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
    descriptors = (
        *_catalog_descriptors_in(connection, catalog_generation_id, normalized_terms),
        *_summary_descriptors_in(connection, normalized_terms, baseline_evidence),
    )
    route_options = tuple(
        DesktopKnowledgeRouteOption(item.route, item.kind, item.title)
        for item in sorted(descriptors, key=_descriptor_sort_key)
    )
    selected = _select_read_descriptors(
        descriptors,
        max_reads=max(0, max_reads),
        excluded_routes=excluded_routes,
        requested_routes=requested_routes,
    )
    reads = tuple(
        read
        for descriptor in selected
        if (read := _resolve_read_in(connection, descriptor)) is not None and read.units
    )
    baseline_ids = {reference.evidence_id for reference in baseline_evidence}
    missing_ids = tuple(
        dict.fromkeys(
            (
                *requested_evidence_ids,
                *_rank_source_evidence_ids_in(
                    connection,
                    reads,
                    terms=normalized_terms,
                    baseline_ids=frozenset(baseline_ids),
                ),
            )
        )
    )
    windows: list[DesktopEvidenceRef] = []
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
            windows.extend(section_evidence)
    snapshot_material = "\n".join(
        (catalog_generation_id, *(f"{read.route}:{read.snapshot_token}" for read in reads))
    )
    snapshot_id = f"navigation-{hashlib.sha256(snapshot_material.encode()).hexdigest()[:24]}"
    return DesktopKnowledgeNavigationResult(
        snapshot_id=snapshot_id,
        reads=reads,
        source_windows=tuple(windows),
        source_read_count=source_read_count,
        route_options=route_options,
    )


def _descriptor_sort_key(item: _ReadDescriptor) -> tuple[int, int, str, str]:
    return (-item.score, item.hop, item.route, item.authority_id)


def _select_read_descriptors(
    descriptors: tuple[_ReadDescriptor, ...],
    *,
    max_reads: int,
    excluded_routes: frozenset[str],
    requested_routes: tuple[str, ...] = (),
) -> tuple[_ReadDescriptor, ...]:
    """Keep one relevant source-backed summary when catalog pages fill the budget."""
    if max_reads <= 0:
        return ()
    by_route = {item.route: item for item in descriptors}
    requested = [by_route[route] for route in dict.fromkeys(requested_routes) if route in by_route][
        :max_reads
    ]
    requested_ids = {item.route for item in requested}
    ranked = sorted(
        (
            item
            for item in descriptors
            if item.route not in excluded_routes and item.route not in requested_ids
        ),
        key=_descriptor_sort_key,
    )
    selected = [*requested, *ranked[: max_reads - len(requested)]]
    if any(item.descriptor_kind == "summary" for item in selected):
        return tuple(selected)
    best_summary = next(
        (item for item in ranked if item.descriptor_kind == "summary"),
        None,
    )
    if best_summary is None:
        return tuple(selected)
    if len(requested) == max_reads:
        return tuple(selected)
    if len(selected) == max_reads:
        selected[-1] = best_summary
    else:
        selected.append(best_summary)
    return tuple(sorted(selected, key=_descriptor_sort_key))


def _catalog_descriptors_in(
    connection: sqlite3.Connection,
    generation_id: str,
    terms: tuple[str, ...],
) -> tuple[_ReadDescriptor, ...]:
    if not terms:
        return ()
    score_expression = " + ".join(
        "CASE WHEN instr(nodes.search_text, ?) > 0 THEN 1 ELSE 0 END" for _term in terms
    )
    qualification = """
        (
            nodes.authority = 'user_revision'
            AND json_extract(nodes.metadata_json, '$.provenance') = 'source_backed'
        ) OR (
            nodes.authority = 'published_generation'
            AND EXISTS (
                SELECT 1 FROM knowledge_generations AS generations
                WHERE generations.generation_id = CAST(
                    json_extract(nodes.metadata_json, '$.generation_id') AS INTEGER
                ) AND generations.qualification_state = 'qualified'
            )
        )
    """
    target_qualification = qualification.replace("nodes.", "targets.")
    rows = connection.execute(
        f"""
        WITH scored AS (
            SELECT nodes.node_id, nodes.kind, nodes.authority, nodes.authority_id,
                nodes.title, nodes.metadata_json, nodes.normalized_title,
                ({score_expression}) AS match_score
            FROM knowledge_catalog_nodes AS nodes
            WHERE nodes.generation_id = ?
                AND nodes.kind IN ('concept', 'entity', 'procedure')
                AND COALESCE(nodes.lifecycle_state, 'stable') != 'deprecated'
                AND ({qualification})
        ), direct AS (
            SELECT *, 0 AS hop FROM scored WHERE match_score > 0
        ), routed AS (
            SELECT * FROM direct
            UNION ALL
            SELECT targets.node_id, targets.kind, targets.authority,
                targets.authority_id, targets.title, targets.metadata_json,
                targets.normalized_title, direct.match_score, 1 AS hop
            FROM direct
            JOIN knowledge_catalog_links AS links
                ON links.generation_id = ? AND links.from_node_id = direct.node_id
            JOIN knowledge_catalog_nodes AS targets
                ON targets.generation_id = links.generation_id
                AND targets.node_id = links.to_node_id
                AND targets.kind IN ('concept', 'entity', 'procedure')
                AND COALESCE(targets.lifecycle_state, 'stable') != 'deprecated'
                AND ({target_qualification})
        )
        SELECT node_id, kind, authority, authority_id, title, metadata_json,
            match_score, hop
        FROM routed
        ORDER BY match_score DESC, hop, normalized_title, node_id
        LIMIT 24
        """,
        (*terms, generation_id, generation_id),
    ).fetchall()
    descriptors: list[_ReadDescriptor] = []
    seen: set[str] = set()
    for row in rows:
        node_id = str(row[0])
        if node_id in seen:
            continue
        seen.add(node_id)
        kind, authority, authority_id = str(row[1]), str(row[2]), str(row[3])
        title, metadata_json = str(row[4]), str(row[5])
        hop = min(int(row[7]), NAVIGATION_MAX_LINK_HOPS)
        route = knowledge_route(kind, authority, title, authority_id)
        descriptors.append(
            _ReadDescriptor(
                score=100 + int(row[6]) * 10 - hop * 20,
                hop=hop,
                descriptor_kind="catalog",
                authority=authority,
                authority_id=authority_id,
                kind=kind,
                title=title,
                metadata_json=metadata_json,
                route=route,
                snapshot_token=metadata_json,
            )
        )
    return tuple(descriptors)


def _summary_descriptors_in(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    baseline_evidence: tuple[DesktopEvidenceRef, ...],
) -> tuple[_ReadDescriptor, ...]:
    baseline_documents = {reference.document_id for reference in baseline_evidence}
    rows = connection.execute(
        """
        SELECT summaries.document_id, documents.display_name, summaries.updated_at,
            GROUP_CONCAT(units.unit_text, ' ')
        FROM document_summaries AS summaries
        JOIN source_documents AS documents ON documents.document_id = summaries.document_id
        JOIN document_summary_units AS units ON units.document_id = summaries.document_id
        WHERE summaries.provenance_state = 'source_backed'
            AND documents.availability = 'available'
        GROUP BY summaries.document_id, documents.display_name, summaries.updated_at
        ORDER BY documents.display_name, summaries.document_id
        """
    ).fetchall()
    descriptors: list[_ReadDescriptor] = []
    for row in rows:
        document_id, title, updated_at = str(row[0]), str(row[1]), str(row[2])
        search_text = f"{title} {row[3] or ''}".casefold()
        term_score = sum(1 for term in terms if term in search_text)
        baseline_bonus = 3 if document_id in baseline_documents else 0
        if term_score == 0 and baseline_bonus == 0:
            continue
        descriptors.append(
            _ReadDescriptor(
                score=60 + term_score * 8 + baseline_bonus,
                hop=0,
                descriptor_kind="summary",
                authority="document_summary",
                authority_id=document_id,
                kind="summary",
                title=title,
                metadata_json="{}",
                route=summary_route(title, document_id),
                snapshot_token=updated_at,
            )
        )
    return tuple(descriptors)


def _resolve_read_in(
    connection: sqlite3.Connection, descriptor: _ReadDescriptor
) -> _NavigationRead | None:
    if descriptor.descriptor_kind == "summary":
        units = _summary_units_in(connection, descriptor.authority_id)
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
    """Rank missing sources by query fit while preserving semantic-unit diversity."""
    streams: list[tuple[tuple[int, int, int, int, str], tuple[str, ...]]] = []
    for read_ordinal, read in enumerate(reads):
        for unit_ordinal, unit in enumerate(read.units):
            unit_matches = _term_match_count(unit.text, terms)
            candidates: list[tuple[tuple[int, int, int, int, str], str]] = []
            for evidence_ordinal, evidence_id in enumerate(unit.evidence_ids):
                if evidence_id in baseline_ids:
                    continue
                relevance = _source_relevance_in(connection, evidence_id, terms)
                if relevance is None:
                    continue
                section_matches, administrative, section = relevance
                # The original section is the routing authority. Summary prose can be broad
                # or multilingual, so it may refine but must not outweigh its own source.
                effective_score = (
                    section_matches * 2 + unit_matches + _guidance_role_bonus(unit.role)
                )
                effective_score -= _unrequested_scope_penalty(
                    section,
                    terms,
                )
                if administrative:
                    effective_score -= 8
                key = (
                    -effective_score,
                    read_ordinal,
                    unit_ordinal,
                    evidence_ordinal,
                    evidence_id,
                )
                candidates.append((key, evidence_id))
            if candidates:
                candidates.sort(key=lambda item: item[0])
                streams.append((candidates[0][0], tuple(item[1] for item in candidates)))
    streams.sort(key=lambda item: item[0])
    ranked: list[str] = []
    positions = [0] * len(streams)
    while True:
        added = False
        for stream_ordinal, (_key, evidence_ids) in enumerate(streams):
            while positions[stream_ordinal] < len(evidence_ids):
                evidence_id = evidence_ids[positions[stream_ordinal]]
                positions[stream_ordinal] += 1
                if evidence_id in ranked:
                    continue
                ranked.append(evidence_id)
                added = True
                break
        if not added:
            break
    return tuple(ranked)


def _source_relevance_in(
    connection: sqlite3.Connection,
    evidence_id: str,
    terms: tuple[str, ...],
) -> tuple[int, bool, str] | None:
    rows = _source_occurrences_in(connection, evidence_id)
    if not rows:
        return None
    best = min(rows, key=lambda row: _source_occurrence_sort_key(row, terms))
    section = _section(str(best[3]))
    return (
        _term_match_count(section, terms),
        _is_administrative_section(section),
        section,
    )


def _guidance_role_bonus(role: str | None) -> int:
    return {"key_topic": 3, "purpose": 1}.get(role or "", 0)


def _unrequested_scope_penalty(section: str, terms: tuple[str, ...]) -> int:
    normalized = section.casefold()
    markers = (
        "扩容",
        "缩容",
        "运维",
        "故障",
        "恢复",
        "升级",
        "附录",
        "faq",
        "maintenance",
        "recovery",
        "upgrade",
        "troubleshoot",
    )
    unmatched = sum(
        marker in normalized and not any(marker in term for term in terms) for marker in markers
    )
    return min(12, unmatched * 6)


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}
