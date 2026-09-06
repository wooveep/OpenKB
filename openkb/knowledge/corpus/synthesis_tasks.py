"""Durable ownership for model-backed corpus synthesis generations."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal

CorpusSynthesisTaskStatus = Literal["running", "completed", "failed", "cancelled", "superseded"]


@dataclass(frozen=True)
class CorpusSynthesisTaskClaim:
    generation_id: int
    execution_token: str
    retry_scope: str | None


def claim_corpus_synthesis_task_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    provider: str,
    model: str,
    retry_scope: str | None,
    now: str,
) -> CorpusSynthesisTaskClaim:
    """Create the generation's durable claim before any provider dispatch."""
    token = uuid.uuid4().hex
    cursor = connection.execute(
        """
        INSERT INTO knowledge_corpus_synthesis_tasks (
            generation_id, status, phase, provider, model, retry_scope,
            execution_token, attempt_count, error_code, error_reason,
            created_at, updated_at, completed_at
        )
        SELECT generation_id, 'running', 'page_planning', ?, ?, ?, ?,
            0, NULL, NULL, ?, ?, NULL
        FROM knowledge_generation_manifests
        WHERE generation_id = ? AND lifecycle_state IN ('pending', 'identity_ready')
        """,
        (provider, model, retry_scope, token, now, now, generation_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Corpus synthesis generation could not be claimed.")
    return CorpusSynthesisTaskClaim(generation_id, token, retry_scope)


def corpus_synthesis_claim_is_active_in(
    connection: sqlite3.Connection,
    claim: CorpusSynthesisTaskClaim,
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM knowledge_corpus_synthesis_tasks "
        "WHERE generation_id = ? AND status = 'running' AND execution_token = ?",
        (claim.generation_id, claim.execution_token),
    ).fetchone()
    return row is not None


def mark_corpus_synthesis_qualification_in(
    connection: sqlite3.Connection,
    claim: CorpusSynthesisTaskClaim,
    *,
    now: str,
) -> bool:
    cursor = connection.execute(
        "UPDATE knowledge_corpus_synthesis_tasks "
        "SET phase = 'qualification', updated_at = ? "
        "WHERE generation_id = ? AND status = 'running' AND execution_token = ?",
        (now, claim.generation_id, claim.execution_token),
    )
    return cursor.rowcount == 1


def record_corpus_synthesis_attempt_in(
    connection: sqlite3.Connection,
    claim: CorpusSynthesisTaskClaim,
    *,
    attempt: int,
    error_code: str | None,
    error_reason: str | None,
    now: str,
) -> bool:
    cursor = connection.execute(
        """
        UPDATE knowledge_corpus_synthesis_tasks
        SET attempt_count = MAX(attempt_count, ?), error_code = ?, error_reason = ?,
            updated_at = ?
        WHERE generation_id = ? AND status = 'running' AND execution_token = ?
        """,
        (
            attempt,
            error_code,
            error_reason[:1000] if error_reason else None,
            now,
            claim.generation_id,
            claim.execution_token,
        ),
    )
    return cursor.rowcount == 1


def finish_corpus_synthesis_task_in(
    connection: sqlite3.Connection,
    claim: CorpusSynthesisTaskClaim,
    *,
    status: CorpusSynthesisTaskStatus,
    error_code: str | None,
    error_reason: str | None,
    now: str,
) -> bool:
    if status == "running":
        raise ValueError("A terminal Corpus synthesis update cannot remain running.")
    cursor = connection.execute(
        """
        UPDATE knowledge_corpus_synthesis_tasks
        SET status = ?, phase = ?, execution_token = NULL,
            error_code = ?, error_reason = ?, updated_at = ?, completed_at = ?
        WHERE generation_id = ? AND status = 'running' AND execution_token = ?
        """,
        (
            status,
            "completed" if status == "completed" else "failed",
            error_code,
            error_reason[:1000] if error_reason else None,
            now,
            now,
            claim.generation_id,
            claim.execution_token,
        ),
    )
    return cursor.rowcount == 1
