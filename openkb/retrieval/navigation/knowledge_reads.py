"""Resolve version-scoped virtual Knowledge Navigation reads."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from openkb.documents.source_sections import section_from_heading_path
from openkb.retrieval.navigation.knowledge_routes import _ReadDescriptor
from openkb.retrieval.rows import json_object
from openkb.retrieval.scoped_evidence import ScopedEvidenceView


@dataclass(frozen=True)
class _GuidanceUnit:
    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _NavigationRead:
    route: str
    kind: str
    authority: str
    title: str
    units: tuple[_GuidanceUnit, ...]
    hop: int
    snapshot_token: str


def resolve_read_in(
    connection: sqlite3.Connection,
    descriptor: _ReadDescriptor,
    *,
    scoped_view: ScopedEvidenceView | None = None,
) -> _NavigationRead | None:
    units: tuple[_GuidanceUnit, ...]
    if descriptor.descriptor_kind == "index":
        units = (_GuidanceUnit(f"Browse the current {descriptor.title.casefold()} routes.", ()),)
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
    if scoped_view is not None:
        units = _scoped_units_in(connection, units, scoped_view)
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


def _scoped_units_in(
    connection: sqlite3.Connection,
    units: tuple[_GuidanceUnit, ...],
    scoped_view: ScopedEvidenceView,
) -> tuple[_GuidanceUnit, ...]:
    values: list[_GuidanceUnit] = []
    for unit in units:
        if not unit.evidence_ids:
            values.append(unit)
            continue
        evidence_ids = tuple(
            evidence_id
            for evidence_id in unit.evidence_ids
            if scoped_view.preferred_occurrence_in(connection, evidence_id) is not None
        )
        if evidence_ids:
            values.append(_GuidanceUnit(unit.text, evidence_ids))
    return tuple(values)


def _summary_units_in(
    connection: sqlite3.Connection, document_id: str
) -> tuple[_GuidanceUnit, ...]:
    rows = connection.execute(
        """
        SELECT units.unit_ordinal, units.unit_text, sources.evidence_id
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
        evidence_index=2,
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
        heading = section_from_heading_path(str(heading_value))
        key = heading.casefold()
        if not heading or key in seen or evidence_value is None:
            continue
        seen.add(key)
        units.append(_GuidanceUnit(heading, (str(evidence_value),)))
    if units:
        return tuple(units)
    if fallback_evidence_id is None:
        return ()
    return (_GuidanceUnit("Document source", (fallback_evidence_id,)),)


def _source_section_units_in(
    connection: sqlite3.Connection, descriptor: _ReadDescriptor
) -> tuple[_GuidanceUnit, ...]:
    metadata = json_object(descriptor.metadata_json)
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
    return (_GuidanceUnit(descriptor.title, evidence_ids),)


def _generated_units_in(
    connection: sqlite3.Connection, descriptor: _ReadDescriptor
) -> tuple[_GuidanceUnit, ...]:
    metadata = json_object(descriptor.metadata_json)
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
    metadata = json_object(descriptor.metadata_json)
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
) -> tuple[_GuidanceUnit, ...]:
    grouped: defaultdict[int, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        grouped[int(str(row[ordinal_index]))].append(row)
    return tuple(
        _GuidanceUnit(
            str(values[0][text_index]),
            tuple(dict.fromkeys(str(value[evidence_index]) for value in values)),
        )
        for _ordinal, values in sorted(grouped.items())
    )
