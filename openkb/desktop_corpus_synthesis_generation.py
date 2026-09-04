"""Generation-scoped corpus manifests, compatibility checks, and atomic activation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from openkb.desktop_candidate_registry import candidate_registry_outcome_in

CORPUS_QUALIFICATION_POLICY_VERSION = "openkb.corpus-qualification.v2"
CORPUS_GENERATION_COMPATIBILITY_VERSION = "openkb.corpus-generation-compatibility.v1"


class CorpusGenerationDependencyError(ValueError):
    """A pending generation cannot pin a complete compatible candidate input set."""


@dataclass(frozen=True)
class CorpusCandidateInput:
    """One exact Candidate Registry Generation selected for corpus synthesis."""

    document_id: str
    candidate_generation_id: str
    candidate_generation_digest: str


@dataclass(frozen=True)
class CorpusGenerationManifest:
    generation_id: int
    parent_generation_id: int | None
    lifecycle_state: str
    dossier_state: str
    graph_state: str
    manifest_digest: str
    compatibility_digest: str
    qualification_policy_version: str
    created_at: str
    updated_at: str


def capture_corpus_candidate_inputs_in(
    connection: sqlite3.Connection,
    *,
    document_ids: tuple[str, ...] = (),
) -> tuple[CorpusCandidateInput, ...]:
    """Resolve current semantic inputs once, before corpus synthesis reads claims."""
    selected_documents = tuple(sorted(dict.fromkeys(document_ids)))
    if not selected_documents:
        selected_documents = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT documents.document_id
                FROM source_documents AS documents
                JOIN knowledge_candidate_registry_state AS state
                  ON state.document_id = documents.document_id
                WHERE documents.availability = 'available'
                  AND state.provenance_state = 'semantic'
                ORDER BY documents.document_id
                """
            ).fetchall()
        )
    inputs: list[CorpusCandidateInput] = []
    for document_id in selected_documents:
        outcome = candidate_registry_outcome_in(connection, document_id)
        if outcome.status not in {"ready", "empty"} or outcome.generation is None:
            raise CorpusGenerationDependencyError(
                f"Candidate Registry Generation is unavailable for {document_id}."
            )
        inputs.append(
            CorpusCandidateInput(
                document_id=document_id,
                candidate_generation_id=outcome.generation.generation_id,
                candidate_generation_digest=outcome.generation.registry_digest,
            )
        )
    if not inputs:
        raise CorpusGenerationDependencyError("No semantic Candidate Registry input is available.")
    return tuple(inputs)


def corpus_candidate_inputs_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[CorpusCandidateInput, ...]:
    rows = connection.execute(
        "SELECT document_id, candidate_generation_id, candidate_generation_digest "
        "FROM knowledge_generation_candidate_inputs WHERE generation_id = ? "
        "ORDER BY document_id",
        (generation_id,),
    ).fetchall()
    return tuple(CorpusCandidateInput(str(row[0]), str(row[1]), str(row[2])) for row in rows)


def create_pending_corpus_manifest_in(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    parent_generation_id: int | None,
    document_ids: tuple[str, ...],
    now: str,
    candidate_inputs: tuple[CorpusCandidateInput, ...] | None = None,
) -> CorpusGenerationManifest:
    """Create the pending publication unit before identity, Dossier, or Graph work."""
    selected = candidate_inputs or capture_corpus_candidate_inputs_in(
        connection, document_ids=document_ids
    )
    expected_documents = tuple(sorted(dict.fromkeys(document_ids)))
    if tuple(item.document_id for item in selected) != expected_documents:
        raise CorpusGenerationDependencyError(
            "Candidate Registry inputs do not cover the selected Document Versions."
        )
    _validate_candidate_inputs_in(connection, selected)
    input_payload = _candidate_input_payload(selected)
    compatibility_digest = _digest(
        {
            "compatibility_version": CORPUS_GENERATION_COMPATIBILITY_VERSION,
            "inputs": input_payload,
        }
    )
    manifest_digest = _digest(
        {
            "compatibility_version": CORPUS_GENERATION_COMPATIBILITY_VERSION,
            "inputs": input_payload,
            "identity_mappings": [],
            "dossiers": [],
            "graph_inputs": [],
            "qualification_policy_version": CORPUS_QUALIFICATION_POLICY_VERSION,
        }
    )
    connection.execute(
        """
        INSERT INTO knowledge_generation_manifests (
            generation_id, parent_generation_id, lifecycle_state, dossier_state,
            graph_state, manifest_digest, compatibility_digest,
            qualification_policy_version, created_at, updated_at
        ) VALUES (?, ?, 'pending', 'pending', 'pending', ?, ?, ?, ?, ?)
        """,
        (
            generation_id,
            parent_generation_id,
            manifest_digest,
            compatibility_digest,
            CORPUS_QUALIFICATION_POLICY_VERSION,
            now,
            now,
        ),
    )
    connection.executemany(
        """
        INSERT INTO knowledge_generation_candidate_inputs (
            generation_id, document_id, candidate_generation_id,
            candidate_generation_digest
        ) VALUES (?, ?, ?, ?)
        """,
        (
            (
                generation_id,
                item.document_id,
                item.candidate_generation_id,
                item.candidate_generation_digest,
            )
            for item in selected
        ),
    )
    manifest = corpus_generation_manifest_in(connection, generation_id)
    if manifest is None:
        raise RuntimeError("Pending corpus manifest disappeared during publication.")
    return manifest


def refresh_corpus_identity_mappings_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    now: str,
) -> None:
    """Bind only candidates from this manifest to identities published in this generation."""
    inputs = corpus_candidate_inputs_in(connection, generation_id)
    mappings = _identity_mappings_in(
        connection,
        generation_id,
        tuple(
            (
                item.document_id,
                item.candidate_generation_id,
                item.candidate_generation_digest,
            )
            for item in inputs
        ),
    )
    connection.execute(
        "DELETE FROM knowledge_generation_identity_mappings WHERE generation_id = ?",
        (generation_id,),
    )
    connection.executemany(
        """
        INSERT INTO knowledge_generation_identity_mappings (
            generation_id, identity_id, candidate_generation_id,
            candidate_id, match_basis
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ((generation_id, *item) for item in mappings),
    )
    connection.execute(
        "UPDATE knowledge_generation_manifests "
        "SET lifecycle_state = CASE WHEN lifecycle_state = 'pending' "
        "THEN 'identity_ready' ELSE lifecycle_state END, updated_at = ? "
        "WHERE generation_id = ?",
        (now, generation_id),
    )
    refresh_corpus_manifest_digest_in(connection, generation_id, now=now)


def bind_generation_graph_inputs_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    now: str,
) -> str:
    """Pin the compatible optional Graph result selected for every candidate input."""
    inputs = corpus_candidate_inputs_in(connection, generation_id)
    connection.execute(
        "DELETE FROM knowledge_generation_graph_inputs WHERE generation_id = ?",
        (generation_id,),
    )
    states: list[str] = []
    for item in inputs:
        admitted = connection.execute(
            "SELECT admitted_count FROM knowledge_candidate_generations "
            "WHERE candidate_generation_id = ?",
            (item.candidate_generation_id,),
        ).fetchone()
        result_id: str | None = None
        if admitted is not None and int(admitted[0]) == 0:
            state = "completed_empty"
        else:
            row = connection.execute(
                """
                SELECT results.result_id, results.status, results.quality
                FROM knowledge_graph_current AS current
                JOIN knowledge_graph_results AS results
                  ON results.result_id = current.result_id
                WHERE current.document_id = ?
                  AND results.candidate_generation_id = ?
                  AND results.candidate_generation_digest = ?
                """,
                (
                    item.document_id,
                    item.candidate_generation_id,
                    item.candidate_generation_digest,
                ),
            ).fetchone()
            if row is None:
                state = "unavailable_optional"
            else:
                result_id = str(row[0])
                state = (
                    "degraded"
                    if str(row[2]) == "degraded"
                    else ("completed_empty" if str(row[1]) == "completed_empty" else "ready")
                )
        states.append(state)
        connection.execute(
            """
            INSERT INTO knowledge_generation_graph_inputs (
                generation_id, document_id, candidate_generation_id,
                candidate_generation_digest, result_id, graph_state
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                item.document_id,
                item.candidate_generation_id,
                item.candidate_generation_digest,
                result_id,
                state,
            ),
        )
    graph_state = _aggregate_graph_state(states)
    connection.execute(
        "UPDATE knowledge_generation_manifests SET graph_state = ?, updated_at = ? "
        "WHERE generation_id = ?",
        (graph_state, now, generation_id),
    )
    refresh_corpus_manifest_digest_in(connection, generation_id, now=now)
    return graph_state


def qualify_corpus_manifest_in(
    connection: sqlite3.Connection, generation_id: int, *, now: str
) -> tuple[str, ...]:
    """Recheck pinned inputs and all generation-owned stage outputs before activation."""
    refresh_corpus_identity_mappings_in(connection, generation_id, now=now)
    manifest = corpus_generation_manifest_in(connection, generation_id)
    if manifest is not None and manifest.dossier_state == "pending":
        entity_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_generation_items "
            "WHERE generation_id = ? AND kind = 'entity'",
            (generation_id,),
        ).fetchone()
        if entity_count is not None and int(entity_count[0]) == 0:
            connection.execute(
                "UPDATE knowledge_generation_manifests SET dossier_state = 'ready' "
                "WHERE generation_id = ?",
                (generation_id,),
            )
    bind_generation_graph_inputs_in(connection, generation_id, now=now)
    refresh_corpus_manifest_digest_in(connection, generation_id, now=now)
    issues = list(corpus_manifest_compatibility_issues_in(connection, generation_id))
    missing_mappings = connection.execute(
        """
        SELECT COUNT(*)
        FROM knowledge_generation_items AS items
        WHERE items.generation_id = ? AND items.identity_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM knowledge_generation_identity_mappings AS mappings
            WHERE mappings.generation_id = items.generation_id
              AND mappings.identity_id = items.identity_id
          )
        """,
        (generation_id,),
    ).fetchone()
    if missing_mappings is not None and int(missing_mappings[0]) > 0:
        issues.append("identity_mapping_incomplete")
    manifest = corpus_generation_manifest_in(connection, generation_id)
    if manifest is None:
        issues.append("manifest_unavailable")
    else:
        if manifest.dossier_state != "ready":
            issues.append("dossier_not_ready")
        if manifest.graph_state == "pending":
            issues.append("graph_not_ready")
    from openkb.desktop_entity_dossier_store import generation_entity_dossier_issues_in

    issues.extend(generation_entity_dossier_issues_in(connection, generation_id))
    connection.execute(
        """
        UPDATE knowledge_generation_manifests
        SET lifecycle_state = ?, updated_at = ? WHERE generation_id = ?
        """,
        ("failed" if issues else "qualified", now, generation_id),
    )
    return tuple(dict.fromkeys(issues))


def fail_corpus_manifest_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    now: str,
    lifecycle_state: str = "failed",
) -> None:
    if lifecycle_state not in {"failed", "cancelled", "superseded"}:
        raise ValueError("Corpus manifest failure requires a terminal lifecycle state.")
    connection.execute(
        "UPDATE knowledge_generation_manifests SET lifecycle_state = ?, updated_at = ? "
        "WHERE generation_id = ? AND lifecycle_state != 'active'",
        (lifecycle_state, now, generation_id),
    )


def recover_interrupted_corpus_generations_in(
    connection: sqlite3.Connection,
    *,
    now: str,
) -> int:
    """Fail orphaned preparation claims on open without dispatching model work."""
    manifest_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'knowledge_generation_manifests'"
    ).fetchone()
    if manifest_table is None:
        return 0
    generation_ids = tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT generation_id FROM knowledge_generation_manifests "
            "WHERE lifecycle_state IN ('pending', 'identity_ready') "
            "ORDER BY generation_id"
        )
    )
    if not generation_ids:
        return 0
    placeholders = ", ".join("?" for _generation_id in generation_ids)
    connection.execute(
        f"UPDATE knowledge_generation_manifests SET lifecycle_state = 'failed', "
        f"dossier_state = CASE WHEN dossier_state = 'pending' THEN 'failed' "
        f"ELSE dossier_state END, updated_at = ? "
        f"WHERE generation_id IN ({placeholders})",
        (now, *generation_ids),
    )
    connection.execute(
        f"UPDATE knowledge_generations SET qualification_state = 'failed' "
        f"WHERE generation_id IN ({placeholders})",
        generation_ids,
    )
    task_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'knowledge_corpus_synthesis_tasks'"
    ).fetchone()
    if task_table is not None:
        connection.execute(
            f"UPDATE knowledge_corpus_synthesis_tasks SET status = 'failed', "
            f"phase = 'failed', execution_token = NULL, "
            f"error_code = 'corpus_synthesis_interrupted', "
            f"error_reason = 'Corpus synthesis was interrupted before publication.', "
            f"updated_at = ?, completed_at = ? "
            f"WHERE generation_id IN ({placeholders}) AND status = 'running'",
            (now, now, *generation_ids),
        )
    return len(generation_ids)


def activate_qualified_corpus_generation_in(
    connection: sqlite3.Connection, generation_id: int, *, now: str
) -> bool:
    """Move the current pointer only while the same manifest remains compatible."""
    manifest = corpus_generation_manifest_in(connection, generation_id)
    if manifest is None or manifest.lifecycle_state != "qualified":
        return False
    from openkb.desktop_entity_dossier_store import generation_entity_dossier_issues_in

    activation_issues = (
        *corpus_manifest_compatibility_issues_in(connection, generation_id),
        *generation_entity_dossier_issues_in(connection, generation_id),
    )
    if manifest.dossier_state != "ready" or manifest.graph_state == "pending" or activation_issues:
        connection.execute(
            "UPDATE knowledge_generation_manifests "
            "SET lifecycle_state = 'superseded', updated_at = ? WHERE generation_id = ?",
            (now, generation_id),
        )
        return False
    previous = connection.execute(
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
    ).fetchone()
    if previous is not None and int(previous[0]) != generation_id:
        connection.execute(
            "UPDATE knowledge_generation_manifests "
            "SET lifecycle_state = 'superseded', updated_at = ? "
            "WHERE generation_id = ? AND lifecycle_state = 'active'",
            (now, int(previous[0])),
        )
    connection.execute(
        """
        INSERT INTO knowledge_generation_state (singleton, current_generation_id)
        VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE
        SET current_generation_id = excluded.current_generation_id
        """,
        (generation_id,),
    )
    connection.execute(
        "UPDATE knowledge_generation_manifests "
        "SET lifecycle_state = 'active', updated_at = ? WHERE generation_id = ?",
        (now, generation_id),
    )
    return True


def refresh_corpus_manifest_digest_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    now: str,
) -> str:
    """Refresh the pending manifest digest after one owned stage is durably written."""
    digest = _digest(_manifest_payload_in(connection, generation_id))
    connection.execute(
        "UPDATE knowledge_generation_manifests SET manifest_digest = ?, updated_at = ? "
        "WHERE generation_id = ? AND lifecycle_state IN ('pending', 'identity_ready')",
        (digest, now, generation_id),
    )
    return digest


def corpus_manifest_compatibility_issues_in(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT inputs.document_id, inputs.candidate_generation_id,
            inputs.candidate_generation_digest, state.provenance_state,
            state.current_candidate_generation_id, generations.registry_digest,
            documents.availability
        FROM knowledge_generation_candidate_inputs AS inputs
        LEFT JOIN source_documents AS documents
          ON documents.document_id = inputs.document_id
        LEFT JOIN knowledge_candidate_registry_state AS state
          ON state.document_id = inputs.document_id
        LEFT JOIN knowledge_candidate_generations AS generations
          ON generations.candidate_generation_id = state.current_candidate_generation_id
        WHERE inputs.generation_id = ? ORDER BY inputs.document_id
        """,
        (generation_id,),
    ).fetchall()
    issues: list[str] = []
    if not rows:
        issues.append("candidate_manifest_empty")
    for row in rows:
        if str(row[6]) != "available" or str(row[3]) != "semantic":
            issues.append("candidate_dependency_unavailable")
        elif str(row[1]) != str(row[4]) or str(row[2]) != str(row[5]):
            issues.append("candidate_generation_superseded")
    graph_rows = connection.execute(
        """
        SELECT graph.document_id, graph.candidate_generation_id,
            graph.candidate_generation_digest, graph.result_id, graph.graph_state,
            inputs.candidate_generation_id, inputs.candidate_generation_digest,
            results.candidate_generation_id, results.candidate_generation_digest
        FROM knowledge_generation_graph_inputs AS graph
        JOIN knowledge_generation_candidate_inputs AS inputs
          ON inputs.generation_id = graph.generation_id
         AND inputs.document_id = graph.document_id
        LEFT JOIN knowledge_graph_results AS results ON results.result_id = graph.result_id
        WHERE graph.generation_id = ? ORDER BY graph.document_id
        """,
        (generation_id,),
    ).fetchall()
    if len(graph_rows) != len(rows):
        issues.append("graph_binding_incomplete")
    for row in graph_rows:
        if str(row[1]) != str(row[5]) or str(row[2]) != str(row[6]):
            issues.append("graph_candidate_generation_mismatch")
        if row[3] is not None and (str(row[1]) != str(row[7]) or str(row[2]) != str(row[8])):
            issues.append("graph_result_generation_mismatch")
        if row[3] is None and str(row[4]) not in {
            "completed_empty",
            "unavailable_optional",
        }:
            issues.append("graph_result_unavailable")
    manifest = corpus_generation_manifest_in(connection, generation_id)
    if manifest is None or manifest.manifest_digest != _digest(
        _manifest_payload_in(connection, generation_id)
    ):
        issues.append("manifest_digest_mismatch")
    return tuple(dict.fromkeys(issues))


def corpus_generation_manifest_in(
    connection: sqlite3.Connection, generation_id: int
) -> CorpusGenerationManifest | None:
    row = connection.execute(
        """
        SELECT generation_id, parent_generation_id, lifecycle_state, dossier_state,
            graph_state, manifest_digest, compatibility_digest,
            qualification_policy_version, created_at, updated_at
        FROM knowledge_generation_manifests WHERE generation_id = ?
        """,
        (generation_id,),
    ).fetchone()
    if row is None:
        return None
    return CorpusGenerationManifest(
        generation_id=int(row[0]),
        parent_generation_id=int(row[1]) if row[1] is not None else None,
        lifecycle_state=str(row[2]),
        dossier_state=str(row[3]),
        graph_state=str(row[4]),
        manifest_digest=str(row[5]),
        compatibility_digest=str(row[6]),
        qualification_policy_version=str(row[7]),
        created_at=str(row[8]),
        updated_at=str(row[9]),
    )


def backfill_corpus_generation_manifests_in(connection: sqlite3.Connection, *, now: str) -> None:
    """Bind historical generations to model-free synthetic candidate snapshots."""
    rows = connection.execute(
        """
        SELECT generations.generation_id, generations.parent_generation_id,
            generations.qualification_state
        FROM knowledge_generations AS generations
        WHERE NOT EXISTS (
            SELECT 1 FROM knowledge_generation_manifests AS manifests
            WHERE manifests.generation_id = generations.generation_id
        )
        ORDER BY generations.generation_id
        """
    ).fetchall()
    current = connection.execute(
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
    ).fetchone()
    current_id = int(current[0]) if current is not None else None
    for row in rows:
        generation_id = int(row[0])
        documents = tuple(
            str(value[0])
            for value in connection.execute(
                "SELECT document_id FROM knowledge_generation_documents "
                "WHERE generation_id = ? ORDER BY document_id",
                (generation_id,),
            ).fetchall()
        )
        if not documents:
            continue
        try:
            create_pending_corpus_manifest_in(
                connection,
                generation_id=generation_id,
                parent_generation_id=int(row[1]) if row[1] is not None else None,
                document_ids=documents,
                now=now,
            )
        except CorpusGenerationDependencyError:
            continue
        refresh_corpus_identity_mappings_in(connection, generation_id, now=now)
        entity_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_generation_items "
            "WHERE generation_id = ? AND kind = 'entity'",
            (generation_id,),
        ).fetchone()
        connection.execute(
            "UPDATE knowledge_generation_manifests SET dossier_state = ?, updated_at = ? "
            "WHERE generation_id = ?",
            (
                "ready" if entity_count is None or int(entity_count[0]) == 0 else "failed",
                now,
                generation_id,
            ),
        )
        bind_generation_graph_inputs_in(connection, generation_id, now=now)
        refresh_corpus_manifest_digest_in(connection, generation_id, now=now)
        lifecycle = (
            "active"
            if generation_id == current_id
            else ("failed" if str(row[2]) == "failed" else "superseded")
        )
        connection.execute(
            "UPDATE knowledge_generation_manifests SET lifecycle_state = ?, updated_at = ? "
            "WHERE generation_id = ?",
            (lifecycle, now, generation_id),
        )


def _identity_mappings_in(
    connection: sqlite3.Connection,
    generation_id: int,
    inputs: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[str, str, str, str], ...]:
    mappings: list[tuple[str, str, str, str]] = []
    for _document_id, candidate_generation_id, _digest_value in inputs:
        rows = connection.execute(
            """
            SELECT mappings.identity_id, mappings.candidate_id, mappings.match_basis
            FROM knowledge_identity_candidates AS mappings
            JOIN knowledge_candidate_generation_candidates AS candidates
              ON candidates.candidate_generation_id = ?
             AND candidates.candidate_id = mappings.candidate_id
            JOIN knowledge_generation_items AS items
              ON items.generation_id = ? AND items.identity_id = mappings.identity_id
            ORDER BY mappings.identity_id, mappings.candidate_id
            """,
            (candidate_generation_id, generation_id),
        ).fetchall()
        mappings.extend(
            (str(row[0]), candidate_generation_id, str(row[1]), str(row[2])) for row in rows
        )
    return tuple(dict.fromkeys(mappings))


def _aggregate_graph_state(states: list[str]) -> str:
    if states and all(state == "completed_empty" for state in states):
        return "completed_empty"
    if states and all(state in {"ready", "completed_empty"} for state in states):
        return "ready"
    if any(state == "degraded" for state in states):
        return "degraded"
    return "unavailable_optional"


def _validate_candidate_inputs_in(
    connection: sqlite3.Connection,
    inputs: tuple[CorpusCandidateInput, ...],
) -> None:
    if not inputs or tuple(sorted(inputs, key=lambda item: item.document_id)) != inputs:
        raise CorpusGenerationDependencyError(
            "Candidate Registry inputs must be a non-empty ordered snapshot."
        )
    if len({item.document_id for item in inputs}) != len(inputs):
        raise CorpusGenerationDependencyError("Candidate Registry inputs contain duplicates.")
    for item in inputs:
        row = connection.execute(
            "SELECT document_id, registry_digest FROM knowledge_candidate_generations "
            "WHERE candidate_generation_id = ?",
            (item.candidate_generation_id,),
        ).fetchone()
        if row is None or (str(row[0]), str(row[1])) != (
            item.document_id,
            item.candidate_generation_digest,
        ):
            raise CorpusGenerationDependencyError(
                f"Candidate Registry Generation is incompatible for {item.document_id}."
            )


def _candidate_input_payload(
    inputs: tuple[CorpusCandidateInput, ...],
) -> list[dict[str, str]]:
    return [
        {
            "document_id": item.document_id,
            "candidate_generation_id": item.candidate_generation_id,
            "candidate_generation_digest": item.candidate_generation_digest,
        }
        for item in inputs
    ]


def _manifest_payload_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> dict[str, object]:
    inputs = corpus_candidate_inputs_in(connection, generation_id)
    mappings = connection.execute(
        """
        SELECT identity_id, candidate_generation_id, candidate_id, match_basis
        FROM knowledge_generation_identity_mappings
        WHERE generation_id = ?
        ORDER BY identity_id, candidate_generation_id, candidate_id
        """,
        (generation_id,),
    ).fetchall()
    dossiers = connection.execute(
        """
        SELECT identity_id, status, plan_digest, planning_operation,
            prompt_contract_digest, rendered_content_digest, fact_count, language
        FROM knowledge_generation_dossier_plans
        WHERE generation_id = ? ORDER BY identity_id
        """,
        (generation_id,),
    ).fetchall()
    graph_inputs = connection.execute(
        """
        SELECT document_id, candidate_generation_id, candidate_generation_digest,
            result_id, graph_state
        FROM knowledge_generation_graph_inputs
        WHERE generation_id = ? ORDER BY document_id
        """,
        (generation_id,),
    ).fetchall()
    return {
        "compatibility_version": CORPUS_GENERATION_COMPATIBILITY_VERSION,
        "inputs": _candidate_input_payload(inputs),
        "identity_mappings": [
            {
                "identity_id": str(row[0]),
                "candidate_generation_id": str(row[1]),
                "candidate_id": str(row[2]),
                "match_basis": str(row[3]),
            }
            for row in mappings
        ],
        "dossiers": [
            {
                "identity_id": str(row[0]),
                "status": str(row[1]),
                "plan_digest": str(row[2]),
                "planning_operation": str(row[3]),
                "prompt_contract_digest": str(row[4]),
                "rendered_content_digest": str(row[5]),
                "fact_count": int(row[6]),
                "language": str(row[7]),
            }
            for row in dossiers
        ],
        "graph_inputs": [
            {
                "document_id": str(row[0]),
                "candidate_generation_id": str(row[1]),
                "candidate_generation_digest": str(row[2]),
                "result_id": str(row[3]) if row[3] is not None else None,
                "graph_state": str(row[4]),
            }
            for row in graph_inputs
        ],
        "qualification_policy_version": CORPUS_QUALIFICATION_POLICY_VERSION,
    }


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
