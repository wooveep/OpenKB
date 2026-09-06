"""Immutable Candidate Registry Generations and their closed dependency outcomes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.knowledge.analysis.evidence_binding import require_applicability_binding
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path

CandidateRegistryStatus = Literal["ready", "empty", "dependency_unavailable"]

CANDIDATE_REGISTRY_SCHEMA_VERSION = "openkb.candidate-registry.v2"
CANDIDATE_NORMALIZER_VERSION = "openkb.knowledge-normalizer.v1"


@dataclass(frozen=True)
class CandidateRegistryGeneration:
    """One immutable, document-scoped candidate dependency snapshot."""

    generation_id: str
    document_id: str
    analysis_provenance_digest: str
    registry_digest: str
    candidate_payload_digest: str
    document_ir_digest: str
    evidence_digest: str
    page_tree_generation_id: str | None
    page_tree_digest: str | None
    analysis_operation: str
    analysis_contract_digest: str
    analysis_prompt_digest: str
    model_capability_provenance_json: str
    candidate_count: int
    admitted_count: int
    claim_count: int
    completion_state: str
    schema_version: str
    normalizer_version: str
    created_at: str


@dataclass(frozen=True)
class CandidateRegistryOutcome:
    """Closed routing result; absence and a valid empty generation never alias."""

    status: CandidateRegistryStatus
    generation: CandidateRegistryGeneration | None = None


class DesktopKnowledgeCandidateRegistry:
    """Read the current marker or an immutable historical generation."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)

    def inspect(self, document_id: str) -> CandidateRegistryOutcome:
        connection = self._connect()
        try:
            return candidate_registry_outcome_in(connection, document_id)
        finally:
            connection.close()

    def generation(self, generation_id: str) -> CandidateRegistryGeneration | None:
        connection = self._connect()
        try:
            return candidate_registry_generation_in(connection, generation_id)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = connect_database(self._database_path)
        return connection


def publish_candidate_registry_generation_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    analysis_provenance_json: str,
    now: str,
) -> CandidateRegistryOutcome:
    """Snapshot the complete current candidate authority inside its owning transaction."""
    document = connection.execute(
        "SELECT 1 FROM source_documents WHERE document_id = ? AND availability = 'available'",
        (document_id,),
    ).fetchone()
    if document is None:
        raise ValueError("Candidate Registry publication requires an Available document.")
    canonical_provenance = _canonical_json_text(analysis_provenance_json)
    candidates = _live_candidate_rows_in(connection, document_id)
    registry_payload = _registry_payload_in(connection, candidates)
    candidate_payload_digest = _digest(registry_payload)
    dependencies = _candidate_dependencies_in(connection, document_id)
    provenance = _analysis_provenance_manifest(canonical_provenance)
    registry_digest = _digest(
        {
            "candidate_payload_digest": candidate_payload_digest,
            **dependencies,
            **provenance,
        }
    )
    generation_id = uuid.uuid4().hex
    admitted_count = sum(str(row[7]) == "admit" for row in candidates)
    claim_count = sum(
        int(
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_document_candidate_claims WHERE candidate_id = ?",
                (str(row[0]),),
            ).fetchone()[0]
        )
        for row in candidates
    )
    completion_state = "ready" if admitted_count else "empty"
    connection.execute(
        """
        INSERT INTO knowledge_candidate_generations (
            candidate_generation_id, document_id, analysis_provenance_json,
            analysis_provenance_digest, registry_digest, candidate_payload_digest,
            document_ir_digest, evidence_digest, page_tree_generation_id,
            page_tree_digest, analysis_operation, analysis_contract_digest,
            analysis_prompt_digest, model_capability_provenance_json,
            candidate_count, admitted_count, claim_count, completion_state,
            schema_version, normalizer_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            generation_id,
            document_id,
            canonical_provenance,
            hashlib.sha256(canonical_provenance.encode("utf-8")).hexdigest(),
            registry_digest,
            candidate_payload_digest,
            dependencies["document_ir_digest"],
            dependencies["evidence_digest"],
            dependencies["page_tree_generation_id"],
            dependencies["page_tree_digest"],
            provenance["analysis_operation"],
            provenance["analysis_contract_digest"],
            provenance["analysis_prompt_digest"],
            provenance["model_capability_provenance_json"],
            len(candidates),
            admitted_count,
            claim_count,
            completion_state,
            CANDIDATE_REGISTRY_SCHEMA_VERSION,
            CANDIDATE_NORMALIZER_VERSION,
            now,
        ),
    )
    for row in candidates:
        connection.execute(
            """
            INSERT INTO knowledge_candidate_generation_candidates (
                candidate_generation_id, candidate_id, kind, title,
                normalized_title, aliases_json, identity_labels_json,
                admission_state, admission_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (generation_id, *row[:6], row[7], row[8]),
        )
        candidate_id = str(row[0])
        claim_rows = connection.execute(
            """
            SELECT claim_ordinal, claim_text, applicability_json
            FROM knowledge_document_candidate_claims
            WHERE candidate_id = ? ORDER BY claim_ordinal
            """,
            (candidate_id,),
        ).fetchall()
        for claim in claim_rows:
            connection.execute(
                """
                INSERT INTO knowledge_candidate_generation_claims (
                    candidate_generation_id, candidate_id, claim_ordinal,
                    claim_text, applicability_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (generation_id, candidate_id, *claim),
            )
            sources = connection.execute(
                """
                SELECT evidence_id FROM knowledge_document_candidate_claim_sources
                WHERE candidate_id = ? AND claim_ordinal = ? ORDER BY evidence_id
                """,
                (candidate_id, int(claim[0])),
            ).fetchall()
            connection.executemany(
                """
                INSERT INTO knowledge_candidate_generation_claim_sources (
                    candidate_generation_id, candidate_id, claim_ordinal, evidence_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (generation_id, candidate_id, int(claim[0]), str(source[0]))
                    for source in sources
                ),
            )
    connection.execute(
        """
        INSERT INTO knowledge_candidate_registry_state (
            document_id, current_candidate_generation_id, updated_at
        ) VALUES (?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            current_candidate_generation_id = excluded.current_candidate_generation_id,
            updated_at = excluded.updated_at
        """,
        (document_id, generation_id, now),
    )
    generation = candidate_registry_generation_in(connection, generation_id)
    if generation is None:
        raise RuntimeError("Candidate Registry generation disappeared during publication.")
    return CandidateRegistryOutcome("ready" if admitted_count else "empty", generation)


def candidate_registry_outcome_in(
    connection: sqlite3.Connection, document_id: str
) -> CandidateRegistryOutcome:
    row = connection.execute(
        """
        SELECT current_candidate_generation_id
        FROM knowledge_candidate_registry_state WHERE document_id = ?
        """,
        (document_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return CandidateRegistryOutcome("dependency_unavailable")
    generation_id = str(row[0])
    generation = candidate_registry_generation_in(connection, generation_id)
    if generation is None or generation.document_id != document_id:
        return CandidateRegistryOutcome("dependency_unavailable")
    try:
        valid = (
            _snapshot_registry_digest_in(connection, generation_id) == generation.registry_digest
        )
    except ValueError:
        valid = False
    if not valid:
        return CandidateRegistryOutcome("dependency_unavailable")
    return CandidateRegistryOutcome("ready" if generation.admitted_count else "empty", generation)


def candidate_registry_generation_in(
    connection: sqlite3.Connection, generation_id: str
) -> CandidateRegistryGeneration | None:
    row = connection.execute(
        """
        SELECT candidate_generation_id, document_id, analysis_provenance_digest,
            registry_digest, candidate_payload_digest, document_ir_digest,
            evidence_digest, page_tree_generation_id, page_tree_digest,
            analysis_operation, analysis_contract_digest, analysis_prompt_digest,
            model_capability_provenance_json, candidate_count, admitted_count,
            claim_count, completion_state, schema_version, normalizer_version, created_at
        FROM knowledge_candidate_generations WHERE candidate_generation_id = ?
        """,
        (generation_id,),
    ).fetchone()
    if row is None:
        return None
    return CandidateRegistryGeneration(
        generation_id=str(row[0]),
        document_id=str(row[1]),
        analysis_provenance_digest=str(row[2]),
        registry_digest=str(row[3]),
        candidate_payload_digest=str(row[4]),
        document_ir_digest=str(row[5]),
        evidence_digest=str(row[6]),
        page_tree_generation_id=str(row[7]) if row[7] is not None else None,
        page_tree_digest=str(row[8]) if row[8] is not None else None,
        analysis_operation=str(row[9]),
        analysis_contract_digest=str(row[10]),
        analysis_prompt_digest=str(row[11]),
        model_capability_provenance_json=str(row[12]),
        candidate_count=int(row[13]),
        admitted_count=int(row[14]),
        claim_count=int(row[15]),
        completion_state=str(row[16]),
        schema_version=str(row[17]),
        normalizer_version=str(row[18]),
        created_at=str(row[19]),
    )


def _live_candidate_rows_in(
    connection: sqlite3.Connection, document_id: str
) -> list[tuple[object, ...]]:
    return connection.execute(
        """
        SELECT candidate_id, kind, title, normalized_title, aliases_json,
            identity_labels_json, analysis_provenance_json,
            admission_state, admission_reason
        FROM knowledge_document_candidates
        WHERE document_id = ? ORDER BY kind, normalized_title, candidate_id
        """,
        (document_id,),
    ).fetchall()


def _registry_payload_in(
    connection: sqlite3.Connection, candidates: list[tuple[object, ...]]
) -> dict[str, object]:
    values: list[dict[str, object]] = []
    for row in candidates:
        candidate_id = str(row[0])
        claims = []
        for claim in connection.execute(
            """
            SELECT claim_ordinal, claim_text, applicability_json
            FROM knowledge_document_candidate_claims
            WHERE candidate_id = ? ORDER BY claim_ordinal
            """,
            (candidate_id,),
        ).fetchall():
            claims.append(
                {
                    "ordinal": int(claim[0]),
                    "text": str(claim[1]),
                    "applicability": _json_value(str(claim[2])),
                    "evidence_ids": [
                        str(source[0])
                        for source in connection.execute(
                            """
                            SELECT evidence_id
                            FROM knowledge_document_candidate_claim_sources
                            WHERE candidate_id = ? AND claim_ordinal = ?
                            ORDER BY evidence_id
                            """,
                            (candidate_id, int(claim[0])),
                        ).fetchall()
                    ],
                }
            )
        for payload in claims:
            require_applicability_binding(payload["applicability"], payload["evidence_ids"])
        values.append(
            {
                "candidate_id": candidate_id,
                "kind": str(row[1]),
                "title": str(row[2]),
                "normalized_title": str(row[3]),
                "aliases": _json_value(str(row[4])),
                "identity_labels": _json_value(str(row[5])),
                "admission_state": str(row[7]),
                "admission_reason": str(row[8]),
                "claims": claims,
            }
        )
    return {
        "schema_version": CANDIDATE_REGISTRY_SCHEMA_VERSION,
        "normalizer_version": CANDIDATE_NORMALIZER_VERSION,
        "candidates": values,
    }


def _snapshot_registry_digest_in(connection: sqlite3.Connection, generation_id: str) -> str:
    candidates = connection.execute(
        """
        SELECT candidate_id, kind, title, normalized_title, aliases_json,
            identity_labels_json, admission_state, admission_reason
        FROM knowledge_candidate_generation_candidates
        WHERE candidate_generation_id = ?
        ORDER BY kind, normalized_title, candidate_id
        """,
        (generation_id,),
    ).fetchall()
    values: list[dict[str, object]] = []
    for row in candidates:
        candidate_id = str(row[0])
        claims = []
        for claim in connection.execute(
            """
            SELECT claim_ordinal, claim_text, applicability_json
            FROM knowledge_candidate_generation_claims
            WHERE candidate_generation_id = ? AND candidate_id = ?
            ORDER BY claim_ordinal
            """,
            (generation_id, candidate_id),
        ).fetchall():
            claims.append(
                {
                    "ordinal": int(claim[0]),
                    "text": str(claim[1]),
                    "applicability": _json_value(str(claim[2])),
                    "evidence_ids": [
                        str(source[0])
                        for source in connection.execute(
                            """
                            SELECT evidence_id
                            FROM knowledge_candidate_generation_claim_sources
                            WHERE candidate_generation_id = ? AND candidate_id = ?
                                AND claim_ordinal = ? ORDER BY evidence_id
                            """,
                            (generation_id, candidate_id, int(claim[0])),
                        ).fetchall()
                    ],
                }
            )
        for payload in claims:
            require_applicability_binding(payload["applicability"], payload["evidence_ids"])
        values.append(
            {
                "candidate_id": candidate_id,
                "kind": str(row[1]),
                "title": str(row[2]),
                "normalized_title": str(row[3]),
                "aliases": _json_value(str(row[4])),
                "identity_labels": _json_value(str(row[5])),
                "admission_state": str(row[6]),
                "admission_reason": str(row[7]),
                "claims": claims,
            }
        )
    candidate_payload_digest = _digest(
        {
            "schema_version": CANDIDATE_REGISTRY_SCHEMA_VERSION,
            "normalizer_version": CANDIDATE_NORMALIZER_VERSION,
            "candidates": values,
        }
    )
    row = connection.execute(
        """
        SELECT document_id, candidate_payload_digest, document_ir_digest,
            evidence_digest, page_tree_generation_id, page_tree_digest,
            analysis_operation, analysis_contract_digest, analysis_prompt_digest,
            model_capability_provenance_json
        FROM knowledge_candidate_generations WHERE candidate_generation_id = ?
        """,
        (generation_id,),
    ).fetchone()
    if row is None or candidate_payload_digest != str(row[1]):
        return ""
    dependencies = _candidate_dependencies_in(connection, str(row[0]))
    stored_dependencies = {
        "document_ir_digest": str(row[2]),
        "evidence_digest": str(row[3]),
        "page_tree_generation_id": str(row[4]) if row[4] is not None else None,
        "page_tree_digest": str(row[5]) if row[5] is not None else None,
    }
    if dependencies != stored_dependencies:
        return ""
    return _digest(
        {
            "candidate_payload_digest": candidate_payload_digest,
            **stored_dependencies,
            "analysis_operation": str(row[6]),
            "analysis_contract_digest": str(row[7]),
            "analysis_prompt_digest": str(row[8]),
            "model_capability_provenance_json": str(row[9]),
        }
    )


def _candidate_dependencies_in(
    connection: sqlite3.Connection, document_id: str
) -> dict[str, object]:
    block_rows = connection.execute(
        """
        SELECT block_id, ordinal, kind, text, heading_path, locator_json
        FROM document_ir_blocks WHERE document_id = ? ORDER BY ordinal, block_id
        """,
        (document_id,),
    ).fetchall()
    evidence_rows = connection.execute(
        """
        SELECT occurrences.block_id, occurrences.ordinal, occurrences.evidence_id,
            evidence.text, evidence.locator_json
        FROM evidence_occurrences AS occurrences
        JOIN evidence_refs AS evidence ON evidence.evidence_id = occurrences.evidence_id
        WHERE occurrences.document_id = ?
        ORDER BY occurrences.ordinal, occurrences.block_id
        """,
        (document_id,),
    ).fetchall()
    page_tree = connection.execute(
        """
        SELECT generations.generation_id, generations.provider_kind,
            generations.provider_version, generations.structural_ir_fingerprint,
            generations.locator_mapping_digest
        FROM document_page_tree_current AS current
        JOIN document_page_tree_generations AS generations
          ON generations.generation_id = current.generation_id
        WHERE current.document_id = ? AND generations.document_id = ?
        """,
        (document_id, document_id),
    ).fetchone()
    page_tree_generation_id = str(page_tree[0]) if page_tree is not None else None
    page_tree_digest = (
        _page_tree_digest_in(connection, page_tree) if page_tree is not None else None
    )
    return {
        "document_ir_digest": _digest(
            [
                {
                    "block_id": str(row[0]),
                    "ordinal": int(row[1]),
                    "kind": str(row[2]),
                    "text": str(row[3]),
                    "heading_path": _json_value(str(row[4])),
                    "locator": _json_value(str(row[5])),
                }
                for row in block_rows
            ]
        ),
        "evidence_digest": _digest(
            [
                {
                    "block_id": str(row[0]),
                    "ordinal": int(row[1]),
                    "evidence_id": str(row[2]),
                    "text": str(row[3]),
                    "locator": _json_value(str(row[4])),
                }
                for row in evidence_rows
            ]
        ),
        "page_tree_generation_id": page_tree_generation_id,
        "page_tree_digest": page_tree_digest,
    }


def _page_tree_digest_in(connection: sqlite3.Connection, generation: tuple[object, ...]) -> str:
    generation_id = str(generation[0])
    nodes = connection.execute(
        """
        SELECT node_id, parent_node_id, node_order, depth, kind, title,
            summary, locator_json
        FROM document_page_tree_nodes WHERE generation_id = ?
        ORDER BY node_order, node_id
        """,
        (generation_id,),
    ).fetchall()
    sources = connection.execute(
        """
        SELECT node_id, evidence_id, block_ordinal, association_order
        FROM document_page_tree_node_evidence WHERE generation_id = ?
        ORDER BY node_id, association_order, evidence_id
        """,
        (generation_id,),
    ).fetchall()
    return _digest(
        {
            "generation": [str(value) for value in generation],
            "nodes": [list(row) for row in nodes],
            "sources": [list(row) for row in sources],
        }
    )


def _analysis_provenance_manifest(canonical_provenance: str) -> dict[str, object]:
    value = _json_value(canonical_provenance)
    payload = value if isinstance(value, dict) else {}
    provenance_digest = hashlib.sha256(canonical_provenance.encode("utf-8")).hexdigest()
    schema_version = str(payload.get("schema_version") or "knowledge_analysis")
    prompt_digest = str(
        payload.get("analysis_prompt_digest") or payload.get("prompt_digest") or provenance_digest
    )
    contract_digest = str(
        payload.get("prompt_contract_digest")
        or payload.get("analysis_contract_digest")
        or _digest({"schema_version": schema_version})
    )
    operation = str(payload.get("analysis_operation") or "knowledge_analysis")
    capability_keys = (
        "provider",
        "model",
        "capability_identity",
        "context_capacity",
        "document_input_capacity",
        "provider_adapter",
        "provider_adapter_version",
        "structured_output_mode",
        "reasoning_effort",
    )
    capability = {key: payload[key] for key in capability_keys if payload.get(key) is not None}
    capability["provenance_digest"] = provenance_digest
    return {
        "analysis_operation": operation,
        "analysis_contract_digest": contract_digest,
        "analysis_prompt_digest": prompt_digest,
        "model_capability_provenance_json": json.dumps(
            capability,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _canonical_json_text(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("Candidate Registry analysis provenance must be valid JSON.") from error
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("Candidate Registry source data contains invalid JSON.") from error


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
