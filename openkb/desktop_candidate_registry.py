"""Immutable Candidate Registry Generations and their closed dependency outcomes."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

CandidateRegistryStatus = Literal["ready", "empty", "dependency_unavailable", "explicit_legacy"]

CANDIDATE_REGISTRY_SCHEMA_VERSION = "openkb.candidate-registry.v1"
CANDIDATE_ONTOLOGY_VERSION = "openkb.knowledge-ontology.v1"
CANDIDATE_NORMALIZER_VERSION = "openkb.knowledge-normalizer.v1"
CANDIDATE_ADMISSION_POLICY_VERSION = "openkb.knowledge-admission.v1"


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
    ontology_version: str
    normalizer_version: str
    admission_policy_version: str
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
        self._state_dir = desktop_state_dir(self._kb_dir)

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

    def mark_explicit_legacy(self, document_id: str) -> CandidateRegistryOutcome:
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                if not _candidate_registry_schema_available_in(connection):
                    # A pre-Candidate-Registry database may still be imported into
                    # before its explicit upgrade.  The additive migration will
                    # persist this marker when that database is next opened.
                    return CandidateRegistryOutcome("dependency_unavailable")
                with connection:
                    mark_candidate_registry_explicit_legacy_in(connection, document_id)
                return candidate_registry_outcome_in(connection, document_id)
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _candidate_registry_schema_available_in(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'knowledge_candidate_registry_state'"
        ).fetchone()
        is not None
    )


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
    admitted_count = sum(str(row[8]) == "admitted" for row in candidates)
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
            schema_version, ontology_version, normalizer_version,
            admission_policy_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            CANDIDATE_ONTOLOGY_VERSION,
            CANDIDATE_NORMALIZER_VERSION,
            CANDIDATE_ADMISSION_POLICY_VERSION,
            now,
        ),
    )
    for row in candidates:
        connection.execute(
            """
            INSERT INTO knowledge_candidate_generation_candidates (
                candidate_generation_id, candidate_id, kind, title,
                normalized_title, entity_subtype, aliases_json, tags_json,
                admission_state, admission_reason, inventory_target_identity_id,
                inventory_target_generation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (generation_id, *row[:7], row[8], row[9], row[10], row[11]),
        )
        candidate_id = str(row[0])
        claim_rows = connection.execute(
            """
            SELECT claim_ordinal, role, claim_text, applicability_json
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
                    role, claim_text, applicability_json
                ) VALUES (?, ?, ?, ?, ?, ?)
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
            document_id, provenance_state, current_candidate_generation_id, updated_at
        ) VALUES (?, 'semantic', ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            provenance_state = excluded.provenance_state,
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
        SELECT provenance_state, current_candidate_generation_id
        FROM knowledge_candidate_registry_state WHERE document_id = ?
        """,
        (document_id,),
    ).fetchone()
    if row is None or str(row[0]) == "dependency_unavailable":
        return CandidateRegistryOutcome("dependency_unavailable")
    if str(row[0]) == "explicit_legacy":
        return CandidateRegistryOutcome("explicit_legacy")
    generation_id = str(row[1]) if row[1] is not None else ""
    generation = candidate_registry_generation_in(connection, generation_id)
    if generation is None or generation.document_id != document_id:
        return CandidateRegistryOutcome("dependency_unavailable")
    if _snapshot_registry_digest_in(connection, generation_id) != generation.registry_digest:
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
            claim_count, completion_state, schema_version, ontology_version,
            normalizer_version, admission_policy_version, created_at
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
        ontology_version=str(row[18]),
        normalizer_version=str(row[19]),
        admission_policy_version=str(row[20]),
        created_at=str(row[21]),
    )


def mark_candidate_registry_explicit_legacy_in(
    connection: sqlite3.Connection, document_id: str, *, now: str | None = None
) -> None:
    """Declare legacy routing only when no semantic generation has been published."""
    stamp = now or _timestamp()
    connection.execute(
        """
        INSERT INTO knowledge_candidate_registry_state (
            document_id, provenance_state, current_candidate_generation_id, updated_at
        )
        SELECT document_id, 'explicit_legacy', NULL, ?
        FROM source_documents WHERE document_id = ?
        ON CONFLICT(document_id) DO UPDATE SET
            provenance_state = 'explicit_legacy',
            current_candidate_generation_id = NULL,
            updated_at = excluded.updated_at
        WHERE knowledge_candidate_registry_state.current_candidate_generation_id IS NULL
        """,
        (stamp, document_id),
    )


def backfill_candidate_registry_generations_in(connection: sqlite3.Connection, *, now: str) -> None:
    """Model-free migration: snapshot semantic rows and explicitly mark older documents."""
    rows = connection.execute(
        "SELECT document_id FROM source_documents ORDER BY document_id"
    ).fetchall()
    for row in rows:
        document_id = str(row[0])
        semantic = connection.execute(
            """
            SELECT 1 FROM knowledge_document_candidates WHERE document_id = ?
            UNION ALL
            SELECT 1 FROM document_summaries WHERE document_id = ?
            LIMIT 1
            """,
            (document_id, document_id),
        ).fetchone()
        if semantic is None:
            mark_candidate_registry_explicit_legacy_in(connection, document_id, now=now)
            continue
        provenance = connection.execute(
            """
            SELECT analysis_provenance_json FROM knowledge_document_candidates
            WHERE document_id = ? ORDER BY candidate_id LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        if provenance is None:
            provenance = connection.execute(
                "SELECT analysis_provenance_json FROM document_summaries WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        publish_candidate_registry_generation_in(
            connection,
            document_id=document_id,
            analysis_provenance_json=(
                str(provenance[0])
                if provenance is not None and provenance[0] is not None
                else '{"migration":"semantic-backfill"}'
            ),
            now=now,
        )
    connection.execute(
        """
        UPDATE knowledge_graph_extraction_tasks
        SET input_provenance = COALESCE((
                SELECT provenance_state FROM knowledge_candidate_registry_state AS state
                WHERE state.document_id = knowledge_graph_extraction_tasks.document_id
            ), 'dependency_unavailable'),
            candidate_generation_id = (
                SELECT current_candidate_generation_id
                FROM knowledge_candidate_registry_state AS state
                WHERE state.document_id = knowledge_graph_extraction_tasks.document_id
            ),
            candidate_generation_digest = (
                SELECT generations.registry_digest
                FROM knowledge_candidate_registry_state AS state
                JOIN knowledge_candidate_generations AS generations
                  ON generations.candidate_generation_id = state.current_candidate_generation_id
                WHERE state.document_id = knowledge_graph_extraction_tasks.document_id
            )
        """
    )
    for table in ("knowledge_graph_results", "knowledge_graph_attempts"):
        connection.execute(
            f"""
            UPDATE {table}
            SET candidate_generation_id = (
                    SELECT current_candidate_generation_id
                    FROM knowledge_candidate_registry_state AS state
                    WHERE state.document_id = {table}.document_id
                      AND state.provenance_state = 'semantic'
                ),
                candidate_generation_digest = (
                    SELECT generations.registry_digest
                    FROM knowledge_candidate_registry_state AS state
                    JOIN knowledge_candidate_generations AS generations
                      ON generations.candidate_generation_id = state.current_candidate_generation_id
                    WHERE state.document_id = {table}.document_id
                )
            """
        )
    connection.execute(
        """
        UPDATE knowledge_document_relationships
        SET candidate_generation_id = (
            SELECT current_candidate_generation_id
            FROM knowledge_candidate_registry_state AS state
            WHERE state.document_id = knowledge_document_relationships.document_id
              AND state.provenance_state = 'semantic'
        )
        """
    )


def _live_candidate_rows_in(
    connection: sqlite3.Connection, document_id: str
) -> list[tuple[object, ...]]:
    return connection.execute(
        """
        SELECT candidate_id, kind, title, normalized_title, entity_subtype,
            aliases_json, tags_json, analysis_provenance_json,
            admission_state, admission_reason,
            inventory_target_identity_id, inventory_target_generation_id
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
            SELECT claim_ordinal, role, claim_text, applicability_json
            FROM knowledge_document_candidate_claims
            WHERE candidate_id = ? ORDER BY claim_ordinal
            """,
            (candidate_id,),
        ).fetchall():
            claims.append(
                {
                    "ordinal": int(claim[0]),
                    "role": str(claim[1]),
                    "text": str(claim[2]),
                    "applicability": _json_value(str(claim[3])),
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
        values.append(
            {
                "candidate_id": candidate_id,
                "kind": str(row[1]),
                "title": str(row[2]),
                "normalized_title": str(row[3]),
                "entity_subtype": str(row[4]) if row[4] is not None else None,
                "aliases": _json_value(str(row[5])),
                "tags": _json_value(str(row[6])),
                "admission_state": str(row[8]),
                "admission_reason": str(row[9]),
                "inventory_target_identity_id": (str(row[10]) if row[10] is not None else None),
                "inventory_target_generation_id": (
                    int(str(row[11])) if row[11] is not None else None
                ),
                "claims": claims,
            }
        )
    return {
        "schema_version": CANDIDATE_REGISTRY_SCHEMA_VERSION,
        "ontology_version": CANDIDATE_ONTOLOGY_VERSION,
        "normalizer_version": CANDIDATE_NORMALIZER_VERSION,
        "admission_policy_version": CANDIDATE_ADMISSION_POLICY_VERSION,
        "candidates": values,
    }


def _snapshot_registry_digest_in(connection: sqlite3.Connection, generation_id: str) -> str:
    candidates = connection.execute(
        """
        SELECT candidate_id, kind, title, normalized_title, entity_subtype,
            aliases_json, tags_json, '', admission_state, admission_reason,
            inventory_target_identity_id, inventory_target_generation_id
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
            SELECT claim_ordinal, role, claim_text, applicability_json
            FROM knowledge_candidate_generation_claims
            WHERE candidate_generation_id = ? AND candidate_id = ?
            ORDER BY claim_ordinal
            """,
            (generation_id, candidate_id),
        ).fetchall():
            claims.append(
                {
                    "ordinal": int(claim[0]),
                    "role": str(claim[1]),
                    "text": str(claim[2]),
                    "applicability": _json_value(str(claim[3])),
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
        values.append(
            {
                "candidate_id": candidate_id,
                "kind": str(row[1]),
                "title": str(row[2]),
                "normalized_title": str(row[3]),
                "entity_subtype": str(row[4]) if row[4] is not None else None,
                "aliases": _json_value(str(row[5])),
                "tags": _json_value(str(row[6])),
                "admission_state": str(row[8]),
                "admission_reason": str(row[9]),
                "inventory_target_identity_id": (str(row[10]) if row[10] is not None else None),
                "inventory_target_generation_id": (
                    int(str(row[11])) if row[11] is not None else None
                ),
                "claims": claims,
            }
        )
    candidate_payload_digest = _digest(
        {
            "schema_version": CANDIDATE_REGISTRY_SCHEMA_VERSION,
            "ontology_version": CANDIDATE_ONTOLOGY_VERSION,
            "normalizer_version": CANDIDATE_NORMALIZER_VERSION,
            "admission_policy_version": CANDIDATE_ADMISSION_POLICY_VERSION,
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


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
