"""Local, content-free Model Usage Records and wait statistics."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_knowledge_analysis_plan import estimate_model_tokens
from openkb.desktop_model_event import normalize_model_event
from openkb.desktop_model_gateway import (
    DesktopModelRequest,
    DesktopProviderTokenUsage,
    ExecutionLane,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

_LONG_WAIT_MINIMUM_SECONDS = 300.0

_PUBLIC_COLUMNS = (
    "call_id",
    "attempt",
    "attempt_id",
    "operation",
    "model_role",
    "provider",
    "model",
    "job_id",
    "stage_run_id",
    "batch_id",
    "execution_lane",
    "lifecycle_status",
    "failure_code",
    "queue_seconds",
    "connect_seconds",
    "first_output_seconds",
    "total_seconds",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "token_usage_source",
    "input_cost",
    "output_cost",
    "total_cost",
    "provider_request_id",
    "created_at",
    "updated_at",
)


class DesktopModelUsageStore:
    """Persist one stable, sanitized representation for every model workload."""

    def __init__(self, kb_dir: Path) -> None:
        resolved = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(resolved)
        self._database_path = desktop_state_database_path(resolved)

    def record_event(
        self,
        *,
        request: DesktopModelRequest,
        event: object,
        provider: str,
        model: str,
    ) -> None:
        """Upsert lifecycle metadata without inspecting or retaining request content."""
        normalized = normalize_model_event(event)
        now = _timestamp()
        attempt_id = f"{normalized.call_id}:{normalized.attempt}"
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO model_usage_records (
                            call_id, attempt, attempt_id, operation, model_role,
                            provider, model, job_id, stage_run_id, batch_id,
                            execution_lane, lifecycle_status, failure_code,
                            attempt_started_elapsed, last_event_elapsed,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(call_id, attempt) DO UPDATE SET
                            lifecycle_status = excluded.lifecycle_status,
                            failure_code = COALESCE(
                                excluded.failure_code,
                                model_usage_records.failure_code
                            ),
                            last_event_elapsed = excluded.last_event_elapsed,
                            updated_at = excluded.updated_at
                        """,
                        (
                            normalized.call_id,
                            normalized.attempt,
                            attempt_id,
                            request.operation,
                            _model_role(request.model_role),
                            provider,
                            model,
                            request.job_id,
                            request.stage_run_id,
                            request.batch_id,
                            _execution_lane(request.execution_lane),
                            normalized.lifecycle_status,
                            normalized.error_code,
                            normalized.elapsed_seconds,
                            normalized.elapsed_seconds,
                            now,
                            now,
                        ),
                    )
                    self._record_phase(connection, normalized)
            finally:
                connection.close()

    def record_result(
        self,
        *,
        request: DesktopModelRequest,
        call_id: str,
        attempt: int,
        content: str,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        usage: DesktopProviderTokenUsage | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        """Attach tokens and optional user-priced cost; bodies are never stored."""
        provider_usage = usage or getattr(content, "usage", None)
        request_id = provider_request_id or getattr(content, "provider_request_id", None)
        if provider_usage is None:
            input_tokens = estimate_model_tokens(request.content)
            output_tokens = estimate_model_tokens(str(content))
            total_tokens = input_tokens + output_tokens
            token_source = "estimated"
        else:
            input_tokens = provider_usage.input_tokens
            output_tokens = provider_usage.output_tokens
            total_tokens = provider_usage.total_tokens
            token_source = "provider_reported"
        input_cost = _cost(input_tokens, input_price_per_million)
        output_cost = _cost(output_tokens, output_price_per_million)
        total_cost = (
            input_cost + output_cost if input_cost is not None and output_cost is not None else None
        )
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE model_usage_records
                        SET input_tokens = ?, output_tokens = ?, total_tokens = ?,
                            token_usage_source = ?, input_cost = ?, output_cost = ?,
                            total_cost = ?, provider_request_id = ?, updated_at = ?
                        WHERE call_id = ? AND attempt = ?
                        """,
                        (
                            input_tokens,
                            output_tokens,
                            total_tokens,
                            token_source,
                            input_cost,
                            output_cost,
                            total_cost,
                            request_id,
                            _timestamp(),
                            call_id,
                            attempt,
                        ),
                    )
            finally:
                connection.close()

    def records(self, *, job_id: str | None = None) -> list[dict[str, object]]:
        query = f"SELECT {', '.join(_PUBLIC_COLUMNS)} FROM model_usage_records"
        values: tuple[object, ...] = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            values = (job_id,)
        query += " ORDER BY created_at, call_id, attempt"
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                return _rows(connection, query, values)
            finally:
                connection.close()

    def aggregate(self, *, job_id: str | None = None) -> dict[str, object]:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                return _model_usage_aggregate(connection, job_id)
            finally:
                connection.close()

    def long_wait_threshold_seconds(self, model_role: str, model: str) -> float:
        """Return max(5 minutes, 2x local completed-call P95)."""
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                return _long_wait_threshold_in(connection, _model_role(model_role), model)
            finally:
                connection.close()

    def _record_phase(self, connection: sqlite3.Connection, event) -> None:
        row = connection.execute(
            """
            SELECT attempt_started_elapsed, connecting_started_elapsed,
                first_output_seconds
            FROM model_usage_records WHERE call_id = ? AND attempt = ?
            """,
            (event.call_id, event.attempt),
        ).fetchone()
        assert row is not None
        started = float(row[0])
        connecting = float(row[1]) if row[1] is not None else None
        status = event.lifecycle_status
        if status == "queued":
            connection.execute(
                """
                UPDATE model_usage_records SET attempt_started_elapsed = ?
                WHERE call_id = ? AND attempt = ?
                """,
                (event.elapsed_seconds, event.call_id, event.attempt),
            )
        elif status == "connecting":
            connection.execute(
                """
                UPDATE model_usage_records
                SET connecting_started_elapsed = ?, queue_seconds = ?
                WHERE call_id = ? AND attempt = ?
                """,
                (
                    event.elapsed_seconds,
                    max(0.0, event.elapsed_seconds - started),
                    event.call_id,
                    event.attempt,
                ),
            )
        elif status == "awaiting_model_result" and connecting is not None:
            connection.execute(
                """
                UPDATE model_usage_records SET connect_seconds = ?
                WHERE call_id = ? AND attempt = ?
                """,
                (
                    max(0.0, event.elapsed_seconds - connecting),
                    event.call_id,
                    event.attempt,
                ),
            )
        elif status == "model_output_activity" and row[2] is None:
            connection.execute(
                """
                UPDATE model_usage_records SET first_output_seconds = ?
                WHERE call_id = ? AND attempt = ?
                """,
                (
                    max(0.0, event.elapsed_seconds - started),
                    event.call_id,
                    event.attempt,
                ),
            )
        if status in {
            "completed",
            "cancelled",
            "provider_failure",
            "network_failure",
        }:
            connection.execute(
                """
                UPDATE model_usage_records SET total_seconds = ?
                WHERE call_id = ? AND attempt = ?
                """,
                (
                    max(0.0, event.elapsed_seconds),
                    event.call_id,
                    event.attempt,
                ),
            )


def _model_role(value: str) -> str:
    return value if value in {"default", "analysis", "answer"} else "default"


def model_usage_records_in(
    connection: sqlite3.Connection,
    job_id: str,
) -> tuple[dict[str, object], ...]:
    return tuple(
        _rows(
            connection,
            f"""
            SELECT {", ".join(_PUBLIC_COLUMNS)} FROM model_usage_records
            WHERE job_id = ? ORDER BY created_at, call_id, attempt
            """,
            (job_id,),
        )
    )


def model_usage_aggregate_in(
    connection: sqlite3.Connection,
    job_id: str,
) -> dict[str, object]:
    return _model_usage_aggregate(connection, job_id)


def _model_usage_aggregate(
    connection: sqlite3.Connection,
    job_id: str | None,
) -> dict[str, object]:
    where = "WHERE job_id = ?" if job_id is not None else ""
    values: tuple[object, ...] = (job_id,) if job_id is not None else ()
    row = connection.execute(
        f"""
        SELECT COUNT(DISTINCT call_id), COUNT(*),
            COUNT(DISTINCT CASE WHEN failure_code IS NOT NULL
                THEN call_id || ':' || attempt END),
            COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
            COALESCE(SUM(total_tokens), 0),
            CASE
                WHEN COUNT(total_tokens) = 0
                    OR COUNT(total_cost) != COUNT(total_tokens) THEN NULL
                ELSE SUM(total_cost)
            END,
            CASE
                WHEN COUNT(token_usage_source) = 0 THEN NULL
                WHEN COUNT(CASE WHEN token_usage_source = 'estimated' THEN 1 END) = 0
                    THEN 'provider_reported'
                WHEN COUNT(CASE WHEN token_usage_source = 'provider_reported' THEN 1 END) = 0
                    THEN 'estimated'
                ELSE 'mixed'
            END
        FROM model_usage_records {where}
        """,
        values,
    ).fetchone()
    assert row is not None
    return {
        "call_count": int(row[0]),
        "attempt_count": int(row[1]),
        "failure_count": int(row[2]),
        "input_tokens": int(row[3]),
        "output_tokens": int(row[4]),
        "total_tokens": int(row[5]),
        "total_cost": float(row[6]) if row[6] is not None else None,
        "token_usage_source": str(row[7]) if row[7] is not None else None,
    }


def current_model_activity_in(
    connection: sqlite3.Connection,
    job_id: str,
) -> dict[str, object] | None:
    row = connection.execute(
        """
        SELECT call_id, attempt, operation, model_role, provider, model, batch_id,
            execution_lane, lifecycle_status, failure_code, last_event_elapsed,
            total_seconds, updated_at
        FROM model_usage_records WHERE job_id = ?
        ORDER BY updated_at DESC, attempt DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return _model_activity_from_row(connection, row)


def model_activity_for_call_in(
    connection: sqlite3.Connection,
    call_id: str,
) -> dict[str, object] | None:
    """Project one non-import Model Call through the same truthful activity contract."""
    row = connection.execute(
        """
        SELECT call_id, attempt, operation, model_role, provider, model, batch_id,
            execution_lane, lifecycle_status, failure_code, last_event_elapsed,
            total_seconds, updated_at
        FROM model_usage_records WHERE call_id = ?
        ORDER BY attempt DESC LIMIT 1
        """,
        (call_id,),
    ).fetchone()
    return _model_activity_from_row(connection, row) if row is not None else None


def _model_activity_from_row(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
) -> dict[str, object]:
    lifecycle = str(row[8])
    active = lifecycle in {
        "queued",
        "connecting",
        "awaiting_model_result",
        "model_output_activity",
        "validating",
        "retrying",
    }
    elapsed = (
        _elapsed_since(str(row[12])) + float(str(row[10])) if active else float(str(row[11] or 0.0))
    )
    threshold = _long_wait_threshold_in(connection, str(row[3]), str(row[5]))
    return {
        "operation": str(row[2]),
        "model_role": str(row[3]),
        "provider": str(row[4]),
        "model": str(row[5]),
        "call_id": str(row[0]),
        "attempt": int(str(row[1])),
        "attempt_id": f"{row[0]}:{row[1]}",
        "batch_id": str(row[6]) if row[6] is not None else None,
        "execution_lane": str(row[7]),
        "status": _user_visible_status(lifecycle),
        "failure_code": str(row[9]) if row[9] is not None else None,
        "elapsed_seconds": elapsed,
        "long_wait_advisory": active and elapsed >= threshold,
        "long_wait_threshold_seconds": threshold,
        "available_actions": (
            ["cancel"]
            if active
            else ["resume"]
            if lifecycle == "cancelled"
            else ["retry"]
            if lifecycle in {"provider_failure", "network_failure"}
            else []
        ),
    }


def _long_wait_threshold_in(
    connection: sqlite3.Connection,
    model_role: str,
    model: str,
) -> float:
    rows = connection.execute(
        """
        SELECT total_seconds FROM model_usage_records
        WHERE model_role = ? AND model = ? AND lifecycle_status = 'completed'
            AND total_seconds IS NOT NULL
        ORDER BY total_seconds
        """,
        (model_role, model),
    ).fetchall()
    if not rows:
        return _LONG_WAIT_MINIMUM_SECONDS
    values = [float(item[0]) for item in rows]
    rank = max(0, math.ceil(0.95 * len(values)) - 1)
    return max(_LONG_WAIT_MINIMUM_SECONDS, 2.0 * values[rank])


def _user_visible_status(lifecycle: str) -> str:
    return {
        "awaiting_model_result": "awaiting_first_result",
        "model_output_activity": "receiving_output",
        "cancelled": "interrupted",
    }.get(lifecycle, lifecycle)


def _elapsed_since(value: str) -> float:
    try:
        created_at = datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(tz=timezone.utc) - created_at).total_seconds())


def _execution_lane(value: ExecutionLane) -> ExecutionLane:
    return value


def _cost(tokens: int, price_per_million: float | None) -> float | None:
    return None if price_per_million is None else tokens * price_per_million / 1_000_000


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _rows(
    connection: sqlite3.Connection,
    query: str,
    values: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    cursor = connection.execute(query, values)
    names = tuple(column[0] for column in cursor.description or ())
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
