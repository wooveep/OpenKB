"""Immutable Candidate Registry reads for corpus synthesis."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from openkb.desktop_corpus_synthesis_generation import CorpusCandidateInput
from openkb.desktop_knowledge_metadata import decode_knowledge_labels
from openkb.desktop_knowledge_rendering import UNSPECIFIED_APPLICABILITY

_APPLICABILITY_DIMENSIONS = (
    "product_version",
    "platform",
    "deployment_scenario",
    "time_boundary",
)


@dataclass(frozen=True)
class CorpusClaim:
    role: str
    text: str
    applicability: tuple[tuple[str, str], ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CorpusCandidate:
    candidate_id: str
    document_id: str
    kind: str
    title: str
    normalized_title: str
    entity_subtype: str | None
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    provenance_json: str
    claims: tuple[CorpusClaim, ...]
    candidate_generation_id: str = "legacy-live-candidate"
    inventory_target_identity_id: str | None = None
    inventory_target_generation_id: int | None = None


def load_admitted_candidates_in(
    connection: sqlite3.Connection,
    inputs: tuple[CorpusCandidateInput, ...],
) -> tuple[CorpusCandidate, ...]:
    """Read admitted candidates only from the immutable generations fixed by a manifest."""
    generation_ids = tuple(item.candidate_generation_id for item in inputs)
    placeholders = ", ".join("?" for _item in generation_ids)
    rows = connection.execute(
        f"""
        SELECT candidates.candidate_generation_id, candidates.candidate_id,
            generations.document_id, candidates.kind, candidates.title,
            candidates.normalized_title, candidates.entity_subtype,
            candidates.aliases_json, candidates.tags_json,
            generations.analysis_provenance_json,
            candidates.inventory_target_identity_id,
            candidates.inventory_target_generation_id
        FROM knowledge_candidate_generation_candidates AS candidates
        JOIN knowledge_candidate_generations AS generations
          ON generations.candidate_generation_id = candidates.candidate_generation_id
        JOIN source_documents AS documents ON documents.document_id = generations.document_id
        WHERE candidates.admission_state = 'admitted'
          AND documents.availability = 'available'
          AND candidates.candidate_generation_id IN ({placeholders})
        ORDER BY candidates.kind, candidates.normalized_title, candidates.candidate_id
        """,
        generation_ids,
    ).fetchall()
    return tuple(_candidate_from_row(connection, row) for row in rows)


def applicability_pairs(value: str) -> tuple[tuple[str, str], ...]:
    """Decode a fixed-dimension applicability snapshot without inventing scope."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    return tuple(
        (dimension, str(payload.get(dimension) or UNSPECIFIED_APPLICABILITY))
        for dimension in _APPLICABILITY_DIMENSIONS
    )


def _candidate_from_row(connection: sqlite3.Connection, row: tuple[object, ...]) -> CorpusCandidate:
    candidate_generation_id = str(row[0])
    candidate_id = str(row[1])
    claim_rows = connection.execute(
        """
        SELECT claims.claim_ordinal, claims.role, claims.claim_text,
            claims.applicability_json, sources.evidence_id
        FROM knowledge_candidate_generation_claims AS claims
        JOIN knowledge_candidate_generation_claim_sources AS sources
          ON sources.candidate_generation_id = claims.candidate_generation_id
         AND sources.candidate_id = claims.candidate_id
         AND sources.claim_ordinal = claims.claim_ordinal
        WHERE claims.candidate_generation_id = ? AND claims.candidate_id = ?
        ORDER BY claims.claim_ordinal, sources.evidence_id
        """,
        (candidate_generation_id, candidate_id),
    ).fetchall()
    grouped: dict[int, list[tuple[object, ...]]] = defaultdict(list)
    for claim_row in claim_rows:
        grouped[int(claim_row[0])].append(claim_row)
    claims = tuple(
        CorpusClaim(
            role=str(values[0][1]),
            text=str(values[0][2]),
            applicability=applicability_pairs(str(values[0][3])),
            evidence_ids=tuple(str(value[4]) for value in values),
        )
        for _ordinal, values in sorted(grouped.items())
    )
    return CorpusCandidate(
        candidate_generation_id=candidate_generation_id,
        candidate_id=candidate_id,
        document_id=str(row[2]),
        kind=str(row[3]),
        title=str(row[4]),
        normalized_title=str(row[5]),
        entity_subtype=str(row[6]) if row[6] is not None else None,
        aliases=decode_knowledge_labels(row[7]),
        tags=decode_knowledge_labels(row[8]),
        provenance_json=str(row[9]),
        claims=claims,
        inventory_target_identity_id=str(row[10]) if row[10] is not None else None,
        inventory_target_generation_id=(int(str(row[11])) if row[11] is not None else None),
    )
