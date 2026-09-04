"""Persist one locally validated document Knowledge Candidate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from openkb.desktop_knowledge_analysis import KnowledgeAnalysisCandidate
from openkb.desktop_knowledge_candidate_admission import assess_knowledge_candidate
from openkb.desktop_knowledge_metadata import encode_knowledge_labels
from openkb.desktop_knowledge_titles import normalize_knowledge_title


def insert_document_candidate_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    candidate: KnowledgeAnalysisCandidate,
    evidence_id_map: Mapping[str, str],
    analysis_provenance_json: str,
    now: str,
) -> None:
    """Validate an Inventory target and persist candidate claims atomically."""
    _require_inventory_identity_target_in(connection, candidate)
    title, normalized_title = normalize_knowledge_title(candidate.title)
    resolved = tuple(
        (
            claim,
            tuple(dict.fromkeys(evidence_id_map[value] for value in claim.source_evidence_ids)),
        )
        for claim in candidate.claims
        if claim.source_evidence_ids
        and all(value in evidence_id_map for value in claim.source_evidence_ids)
    )
    admission = assess_knowledge_candidate(
        kind=candidate.kind,
        title=title,
        subtype=candidate.subtype,
        claims=tuple((claim.role, claim.text) for claim, _sources in resolved),
        decision_reasons=candidate.admission_reason_codes,
    )
    admitted = admission.admitted and candidate.inventory_decision not in {"review", "reject"}
    admission_reason = (
        admission.reason
        if not admission.admitted or candidate.inventory_decision not in {"review", "reject"}
        else candidate.admission_reason_codes[0]
        if candidate.admission_reason_codes
        else f"inventory_{candidate.inventory_decision}"
    )
    candidate_id = hashlib.sha256(
        f"{document_id}\x1f{candidate.kind}\x1f{normalized_title}".encode("utf-8")
    ).hexdigest()
    connection.execute(
        """
        INSERT INTO knowledge_document_candidates (
            candidate_id, document_id, kind, title, normalized_title,
            entity_subtype, aliases_json, tags_json, admission_state,
            admission_reason, analysis_provenance_json, created_at,
            inventory_target_identity_id, inventory_target_generation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            document_id,
            candidate.kind,
            title,
            normalized_title,
            candidate.subtype,
            encode_knowledge_labels(candidate.aliases),
            encode_knowledge_labels(candidate.tags),
            "admitted" if admitted else "rejected",
            admission_reason,
            analysis_provenance_json,
            now,
            candidate.inventory_target_identity_id,
            candidate.inventory_target_generation_id,
        ),
    )
    for ordinal, (claim, source_ids) in enumerate(resolved):
        connection.execute(
            """
            INSERT INTO knowledge_document_candidate_claims (
                candidate_id, claim_ordinal, role, claim_text, applicability_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                ordinal,
                claim.role,
                claim.text,
                _json(claim.applicability.as_dict()),
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_document_candidate_claim_sources (
                candidate_id, claim_ordinal, evidence_id
            ) VALUES (?, ?, ?)
            """,
            ((candidate_id, ordinal, evidence_id) for evidence_id in source_ids),
        )


def _require_inventory_identity_target_in(
    connection: sqlite3.Connection,
    candidate: KnowledgeAnalysisCandidate,
) -> None:
    target_identity_id = candidate.inventory_target_identity_id
    target_generation_id = candidate.inventory_target_generation_id
    if candidate.inventory_decision not in {"update", "alias"}:
        if target_identity_id is not None or target_generation_id is not None:
            raise ValueError("Only update or alias may carry an Inventory target identity.")
        return
    if target_identity_id is None or target_generation_id is None:
        raise ValueError("Inventory update or alias requires a target identity generation.")
    row = connection.execute(
        """
        SELECT 1
        FROM knowledge_generation_state AS state
        JOIN knowledge_generation_items AS items
          ON items.generation_id = state.current_generation_id
        JOIN knowledge_identities AS identities
          ON identities.identity_id = items.identity_id
        WHERE state.singleton = 1 AND state.current_generation_id = ?
          AND items.identity_id = ? AND items.kind = 'entity'
          AND identities.status = 'active'
        """,
        (target_generation_id, target_identity_id),
    ).fetchone()
    if row is None:
        raise ValueError("Inventory target identity generation is no longer current.")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
