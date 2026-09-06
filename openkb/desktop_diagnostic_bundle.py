"""User-initiated, redacted Desktop diagnostic bundle export."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from openkb import __version__
from openkb.desktop_diagnostic_logs import diagnostic_log_payloads
from openkb.desktop_model_settings import read_desktop_model_settings
from openkb.desktop_source_integrity import audit_source_integrity_in
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseError,
    desktop_state_database_path,
    desktop_state_dir,
)
from openkb.locks import kb_ingest_lock


class DesktopDiagnosticBundleError(DesktopKnowledgeBaseError):
    """An export failure that reveals neither content nor credential values."""

    def __init__(self, message: str) -> None:
        super().__init__("desktop_diagnostic_bundle_failed", message)


@dataclass(frozen=True)
class DesktopDiagnosticBundle:
    path: str
    files: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "files": list(self.files)}


class DesktopDiagnosticBundleService:
    """Write a small, reviewable support bundle only after an explicit request."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)

    def export(self, destination: Path) -> DesktopDiagnosticBundle:
        target = destination.expanduser()
        if target.suffix.lower() != ".zip":
            raise DesktopDiagnosticBundleError("Choose a .zip path for the diagnostic bundle.")
        if not target.parent.is_dir():
            raise DesktopDiagnosticBundleError(
                "The selected diagnostic-bundle folder is unavailable."
            )
        payloads = self._payloads()
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".zip",
                prefix=".openkb-diagnostic-",
                dir=target.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, payload in payloads.items():
                    archive.writestr(
                        name,
                        payload
                        if isinstance(payload, bytes)
                        else json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                    )
            os.replace(temporary_path, target)
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as error:
            raise DesktopDiagnosticBundleError(
                "Desktop diagnostics could not be exported."
            ) from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return DesktopDiagnosticBundle(path=str(target), files=tuple(payloads))

    def _payloads(self) -> dict[str, object | bytes]:
        if not self._database_path.is_file():
            raise DesktopDiagnosticBundleError(
                "The active Desktop Knowledge Base state is unavailable."
            )
        with kb_ingest_lock(desktop_state_dir(self._kb_dir)):
            connection = sqlite3.connect(self._database_path)
            try:
                schema_version = _scalar(connection, "SELECT MAX(version) FROM schema_migrations")
                payloads: dict[str, object | bytes] = {
                    "manifest.json": {
                        "format": "openkb-desktop-diagnostic-bundle-v3",
                        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "openkb_version": __version__,
                        "schema_version": schema_version,
                        "redaction": {
                            "source_file_content": "excluded",
                            "model_request_response_bodies": "excluded",
                            "credentials_and_headers": "excluded",
                            "sensitive_trace_captures": "excluded",
                            "application_logs": "support_safe_tail_only",
                        },
                    },
                    "model-settings.json": read_desktop_model_settings(
                        self._kb_dir
                    ).as_diagnostic_dict(),
                    "import-jobs.json": {
                        "jobs": _rows(
                            connection,
                            """
                            SELECT job_id, document_id, status, progress, error_code,
                                created_at, completed_at
                            FROM import_jobs ORDER BY created_at DESC
                            """,
                        ),
                        "stages": _rows(
                            connection,
                            """
                            SELECT job_id, stage, status, progress, error_code,
                                started_at, completed_at
                            FROM stage_runs ORDER BY job_id, stage
                            """,
                        ),
                        "quarantines": _rows(
                            connection,
                            """
                            SELECT job_id, stage, error_code, attempt_count, created_at
                            FROM quarantined_documents ORDER BY created_at DESC
                            """,
                        ),
                    },
                    "model-calls.json": {
                        "calls": _rows(
                            connection,
                            """
                            SELECT call_id, job_id, stage_run_id, operation, status, attempt_count,
                                error_code, finish_reason,
                                reasoning_observed, final_content_observed,
                                reasoning_chunk_count, final_chunk_count,
                                reasoning_character_count, final_character_count,
                                input_tokens, output_tokens, total_tokens, provider_request_id,
                                created_at, completed_at
                            FROM model_calls ORDER BY created_at DESC
                            """,
                        ),
                        "attempts": _rows(
                            connection,
                            """
                            SELECT call_id, attempt, status, error_code, finish_reason,
                                reasoning_observed, final_content_observed,
                                reasoning_chunk_count, final_chunk_count,
                                reasoning_character_count, final_character_count,
                                input_tokens, output_tokens, total_tokens, provider_request_id,
                                created_at, completed_at
                            FROM model_attempts ORDER BY call_id, attempt
                            """,
                        ),
                    },
                    "model-usage.json": {
                        "records": _rows(
                            connection,
                            """
                            SELECT call_id, attempt, attempt_id, operation, model_role,
                                provider, model, job_id, stage_run_id, batch_id,
                                execution_lane, lifecycle_status, failure_code, queue_seconds,
                                connect_seconds, first_output_seconds, total_seconds,
                                finish_reason, reasoning_observed, final_content_observed,
                                reasoning_chunk_count, final_chunk_count,
                                reasoning_character_count, final_character_count,
                                input_tokens, output_tokens, total_tokens,
                                token_usage_source, input_cost, output_cost, total_cost,
                                provider_request_id, created_at, updated_at
                            FROM model_usage_records
                            ORDER BY created_at DESC, call_id, attempt
                            """,
                        ),
                        "aggregate": _row(
                            connection,
                            """
                            SELECT COUNT(DISTINCT call_id) AS call_count,
                                COUNT(*) AS attempt_count,
                                COUNT(DISTINCT CASE WHEN failure_code IS NOT NULL
                                    THEN call_id || ':' || attempt END) AS failure_count,
                                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                                CASE WHEN COUNT(total_cost) = 0
                                    THEN NULL ELSE SUM(total_cost) END AS total_cost
                            FROM model_usage_records
                            """,
                        ),
                    },
                    "model-operation-contracts.json": {
                        "contracts": _rows(
                            connection,
                            """
                            SELECT operation, capability_identity, prompt_contract_digest,
                                status, failure_code, failure_stage, failure_signature,
                                CASE
                                    WHEN operation IN (
                                        'query_planning',
                                        'page_tree_selection',
                                        'knowledge_navigation_step'
                                    )
                                        THEN 'regenerate_answer'
                                    WHEN operation = 'knowledge_relation_analysis'
                                        THEN 'retry_graph_extraction'
                                    WHEN operation = 'page_tree_enrichment'
                                        THEN 'retry_page_tree_enrichment'
                                    WHEN operation IN (
                                        'knowledge_fact_harvest',
                                        'knowledge_analysis', 'knowledge_analysis_batch',
                                        'knowledge_analysis_merge'
                                    ) THEN 'recover_import_or_start_reanalysis'
                                    WHEN operation = 'structured_output_repair'
                                        THEN 'retry_parent_operation'
                                    ELSE 'verify_model_configuration'
                                END AS recovery_action,
                                created_at, updated_at
                            FROM model_operation_contract_states
                            ORDER BY updated_at DESC, operation, prompt_contract_digest
                            """,
                        ),
                        "retry_permits": _rows(
                            connection,
                            """
                            SELECT operation, capability_identity, prompt_contract_digest,
                                retry_scope, created_at
                            FROM model_operation_retry_permits
                            ORDER BY created_at DESC, operation, prompt_contract_digest
                            """,
                        ),
                        "events": _rows(
                            connection,
                            """
                            SELECT event_id, operation, capability_identity,
                                prompt_contract_digest, status, failure_code,
                                failure_stage, failure_signature,
                                CASE
                                    WHEN operation IN (
                                        'query_planning',
                                        'page_tree_selection',
                                        'knowledge_navigation_step'
                                    )
                                        THEN 'regenerate_answer'
                                    WHEN operation = 'knowledge_relation_analysis'
                                        THEN 'retry_graph_extraction'
                                    WHEN operation = 'page_tree_enrichment'
                                        THEN 'retry_page_tree_enrichment'
                                    WHEN operation IN (
                                        'knowledge_fact_harvest',
                                        'knowledge_analysis', 'knowledge_analysis_batch',
                                        'knowledge_analysis_merge'
                                    ) THEN 'recover_import_or_start_reanalysis'
                                    WHEN operation = 'structured_output_repair'
                                        THEN 'retry_parent_operation'
                                    ELSE 'verify_model_configuration'
                                END AS recovery_action,
                                created_at
                            FROM model_operation_contract_events
                            ORDER BY event_id DESC
                            """,
                        ),
                    },
                    "graph-diagnostics.json": {
                        "diagnostics": _rows(
                            connection,
                            """
                            SELECT phase, error_code, document_id, created_at
                            FROM knowledge_graph_diagnostics ORDER BY created_at DESC
                            """,
                        ),
                        "feature_flags": _rows(
                            connection,
                            """
                            SELECT feature_key, enabled, approved_snapshot_revision, updated_at
                            FROM desktop_graph_feature_flags ORDER BY feature_key
                            """,
                        ),
                        "results": _rows(
                            connection,
                            """
                            SELECT result_id, document_id, status, capability_identity,
                                prompt_contract_digest, node_count, edge_count,
                                quality, retained_count,
                                weakened_count, rejected_count, document_version,
                                evidence_snapshot_digest, canonical_schema_version,
                                normalizer_version, verification_policy_version,
                                candidate_generation_id, candidate_generation_digest,
                                created_at
                            FROM knowledge_graph_results
                            ORDER BY created_at DESC, result_id
                            """,
                        ),
                        "current_results": _rows(
                            connection,
                            """
                            SELECT current.document_id, current.result_id, results.status,
                                results.node_count, results.edge_count, results.quality,
                                results.retained_count, results.weakened_count,
                                results.rejected_count
                            FROM knowledge_graph_current AS current
                            JOIN knowledge_graph_results AS results
                                ON results.result_id = current.result_id
                            ORDER BY current.document_id
                            """,
                        ),
                    },
                    "page-tree-enrichment.json": {
                        "tasks": _rows(
                            connection,
                            """
                            SELECT document_id, base_generation_id, status,
                                provider, model, attempt_count, model_attempt, call_id,
                                error_code, created_at, updated_at, completed_at
                            FROM document_page_tree_enrichment_tasks
                            ORDER BY updated_at DESC
                            """,
                        ),
                        "generations": _rows(
                            connection,
                            """
                            SELECT enrichment_generation_id, document_id, base_generation_id,
                                provider, model, prompt_digest, status, created_at
                            FROM document_page_tree_enrichment_generations
                            ORDER BY created_at DESC
                            """,
                        ),
                    },
                    "integrity.json": {
                        "source_integrity": audit_source_integrity_in(
                            connection, kb_dir=self._kb_dir
                        ).as_dict(),
                        "source_document_counts": _rows(
                            connection,
                            """
                            SELECT availability, COUNT(*) AS count
                            FROM source_documents GROUP BY availability ORDER BY availability
                            """,
                        ),
                        "raw_asset_counts": _rows(
                            connection,
                            """
                            SELECT lifecycle_status, COUNT(*) AS count
                            FROM raw_asset_integrity
                            GROUP BY lifecycle_status
                            ORDER BY lifecycle_status
                            """,
                        ),
                    },
                }
            except sqlite3.Error as error:
                raise DesktopDiagnosticBundleError(
                    "Desktop diagnostics are unavailable."
                ) from error
            finally:
                connection.close()
        payloads.update(diagnostic_log_payloads())
        return payloads


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, object]]:
    cursor = connection.execute(query)
    names = tuple(column[0] for column in cursor.description or ())
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _scalar(connection: sqlite3.Connection, query: str) -> object:
    row = connection.execute(query).fetchone()
    return row[0] if row is not None else None


def _row(connection: sqlite3.Connection, query: str) -> dict[str, object]:
    rows = _rows(connection, query)
    return rows[0] if rows else {}
