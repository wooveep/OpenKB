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
from openkb.desktop_model_settings import read_desktop_model_settings
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
                        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
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

    def _payloads(self) -> dict[str, object]:
        if not self._database_path.is_file():
            raise DesktopDiagnosticBundleError(
                "The active Desktop Knowledge Base state is unavailable."
            )
        with kb_ingest_lock(desktop_state_dir(self._kb_dir)):
            connection = sqlite3.connect(self._database_path)
            try:
                schema_version = _scalar(connection, "SELECT MAX(version) FROM schema_migrations")
                payloads: dict[str, object] = {
                    "manifest.json": {
                        "format": "openkb-desktop-diagnostic-bundle-v1",
                        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "openkb_version": __version__,
                        "schema_version": schema_version,
                        "redaction": {
                            "source_file_content": "excluded",
                            "model_request_response_bodies": "excluded",
                            "credentials_and_headers": "excluded",
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
                            SELECT job_id, stage, error_code, reason, suggested_action,
                                attempt_count, created_at
                            FROM quarantined_documents ORDER BY created_at DESC
                            """,
                        ),
                    },
                    "model-calls.json": {
                        "calls": _rows(
                            connection,
                            """
                            SELECT call_id, job_id, stage_run_id, operation, status, attempt_count,
                                timeout_seconds, next_timeout_seconds, remaining_seconds,
                                error_code,
                                reason, suggested_action, created_at, completed_at
                            FROM model_calls ORDER BY created_at DESC
                            """,
                        ),
                        "attempts": _rows(
                            connection,
                            """
                            SELECT call_id, attempt, status, timeout_seconds, remaining_seconds,
                                error_code, reason, created_at, completed_at
                            FROM model_attempts ORDER BY call_id, attempt
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
                    },
                    "integrity.json": {
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
        return payloads


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, object]]:
    cursor = connection.execute(query)
    names = tuple(column[0] for column in cursor.description or ())
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _scalar(connection: sqlite3.Connection, query: str) -> object:
    row = connection.execute(query).fetchone()
    return row[0] if row is not None else None
