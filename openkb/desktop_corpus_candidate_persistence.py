"""Persist one locally validated document Knowledge Candidate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from openkb.desktop_knowledge_analysis import KnowledgeAnalysisCandidate
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
    """Persist a validated model candidate and its evidence-bound claims atomically."""
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
    candidate_id = hashlib.sha256(
        f"{document_id}\x1f{candidate.kind}\x1f{normalized_title}".encode("utf-8")
    ).hexdigest()
    connection.execute(
        """
        INSERT INTO knowledge_document_candidates (
            candidate_id, document_id, kind, title, normalized_title,
            aliases_json, identity_labels_json, admission_state,
            admission_reason, analysis_provenance_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            document_id,
            candidate.kind,
            title,
            normalized_title,
            encode_knowledge_labels(candidate.aliases),
            encode_knowledge_labels(candidate.identity_labels),
            candidate.admission,
            f"model_{candidate.admission}",
            analysis_provenance_json,
            now,
        ),
    )
    for ordinal, (claim, source_ids) in enumerate(resolved):
        connection.execute(
            """
            INSERT INTO knowledge_document_candidate_claims (
                candidate_id, claim_ordinal, claim_text, applicability_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                candidate_id,
                ordinal,
                claim.text,
                _json([entry.as_dict() for entry in claim.applicability]),
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


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
