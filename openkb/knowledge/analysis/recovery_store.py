"""Checkpoint operations shared by explicit and legacy model recovery.

These operations run inside the caller's existing locked transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from openkb.knowledge.analysis.plan import KnowledgeAnalysisPlan


def discard_analysis_in(connection: sqlite3.Connection, job_id: str) -> None:
    """Discard model checkpoints in foreign-key order for one import job."""
    connection.execute("DELETE FROM knowledge_analysis_merge_nodes WHERE job_id = ?", (job_id,))
    connection.execute("DELETE FROM knowledge_analysis_merges WHERE job_id = ?", (job_id,))
    connection.execute("DELETE FROM knowledge_analysis_batches WHERE job_id = ?", (job_id,))
    connection.execute("DELETE FROM knowledge_analysis_plans WHERE job_id = ?", (job_id,))


def plan_identity_in(connection: sqlite3.Connection, job_id: str) -> str | None:
    """Read a current identity or preserve a historical plan's audit digest."""
    row = connection.execute(
        "SELECT plan_json FROM knowledge_analysis_plans WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return None
    serialized = str(row[0])
    try:
        return KnowledgeAnalysisPlan.from_dict(json.loads(serialized)).plan_identity
    except (json.JSONDecodeError, ValueError):
        # The digest is audit context, not permission to resume an old plan.
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
