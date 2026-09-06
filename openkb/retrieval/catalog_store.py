"""Crash-safe ownership, rebuilds, and leases for corpus Catalog generations."""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from openkb.locks import kb_ingest_lock
from openkb.retrieval.catalog_snapshot import (
    CatalogLink,
    CatalogSnapshot,
    build_catalog_snapshot_in,
)
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

logger = logging.getLogger(__name__)
CATALOG_BUILD_ERROR_CODE = "knowledge_catalog_build_failed"
_MAX_CATALOG_BUILD_ATTEMPTS = 2
_WORKER_LOCK = threading.Lock()
_ACTIVE_WORKERS: set[Path] = set()
_RERUN_REQUESTS: set[Path] = set()
_RECOVERY_REQUESTS: set[Path] = set()
_LEASE_LOCK = threading.Lock()
_GENERATION_LEASES: dict[tuple[Path, str], int] = {}


@dataclass(frozen=True)
class CatalogGenerationLease:
    generation_id: str
    source_revision: int
    is_stale: bool


@dataclass(frozen=True)
class _CatalogClaim:
    execution_token: str
    source_revision: int


class _ObsoleteCatalogClaim(RuntimeError):
    pass


def queue_catalog_rebuild_in(connection: sqlite3.Connection, reason: str) -> int:
    """Invalidate the current Catalog inside one owning authority transaction."""
    now = _timestamp()
    connection.execute(
        """
        UPDATE knowledge_catalog_state
        SET source_revision = source_revision + 1, is_stale = 1,
            stale_since = COALESCE(stale_since, ?)
        WHERE singleton = 1
        """,
        (now,),
    )
    row = connection.execute(
        "SELECT source_revision FROM knowledge_catalog_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("Knowledge Catalog state is missing.")
    source_revision = int(row[0])
    connection.execute(
        """
        INSERT INTO knowledge_catalog_rebuild_tasks (
            singleton, status, reason, requested_source_revision, execution_token,
            attempt_count, error_code, error_reason, created_at, updated_at, completed_at
        ) VALUES (1, 'pending', ?, ?, NULL, 0, NULL, NULL, ?, ?, NULL)
        ON CONFLICT(singleton) DO UPDATE SET
            status = 'pending', reason = excluded.reason,
            requested_source_revision = excluded.requested_source_revision,
            execution_token = NULL, attempt_count = 0,
            error_code = NULL, error_reason = NULL,
            updated_at = excluded.updated_at, completed_at = NULL
        """,
        (reason, source_revision, now, now),
    )
    return source_revision


def recover_catalog_rebuilds(kb_dir: Path, *, retry_failed: bool = True) -> None:
    """Converge an interrupted process lease without discarding a stale generation."""
    resolved = kb_dir.expanduser().resolve()
    with kb_ingest_lock(desktop_state_dir(resolved)):
        connection = _connect(desktop_state_database_path(resolved))
        try:
            with connection:
                statuses = ("running", "failed") if retry_failed else ("running",)
                placeholders = ", ".join("?" for _ in statuses)
                connection.execute(
                    f"""
                    UPDATE knowledge_catalog_rebuild_tasks
                    SET status = 'pending', execution_token = NULL, error_code = NULL,
                        error_reason = NULL, updated_at = ?, completed_at = NULL
                    WHERE status IN ({placeholders})
                    """,
                    (_timestamp(), *statuses),
                )
        finally:
            connection.close()


def start_catalog_rebuilds(kb_dir: Path, *, recover: bool = False) -> None:
    """Start one coalescing daemon for committed Catalog invalidations."""
    resolved = kb_dir.expanduser().resolve()
    if recover:
        with _WORKER_LOCK:
            if resolved in _ACTIVE_WORKERS:
                _RECOVERY_REQUESTS.add(resolved)
                _RERUN_REQUESTS.add(resolved)
                return
    try:
        if recover:
            recover_catalog_rebuilds(resolved)
        if not _has_pending_work(resolved):
            return
    except (OSError, sqlite3.Error):
        logger.warning("Could not inspect Knowledge Catalog rebuild work.", exc_info=True)
        return
    with _WORKER_LOCK:
        if resolved in _ACTIVE_WORKERS:
            _RERUN_REQUESTS.add(resolved)
            return
        _ACTIVE_WORKERS.add(resolved)
    try:
        threading.Thread(
            target=_run_catalog_worker,
            args=(resolved,),
            daemon=True,
            name="openkb-catalog-rebuild",
        ).start()
    except RuntimeError:
        with _WORKER_LOCK:
            _ACTIVE_WORKERS.discard(resolved)
            _RERUN_REQUESTS.discard(resolved)
            _RECOVERY_REQUESTS.discard(resolved)
        logger.warning("Could not start Knowledge Catalog rebuild worker.", exc_info=True)


def rebuild_pending_catalog(kb_dir: Path) -> bool:
    """Consume each committed target once; return whether a generation was activated."""
    resolved = kb_dir.expanduser().resolve()
    activated = False
    while True:
        claim = _claim_catalog_rebuild(resolved)
        if claim is None:
            return activated
        try:
            snapshot = _build_claim_snapshot(resolved, claim)
            activated = _publish_claim_snapshot(resolved, claim, snapshot) or activated
        except _ObsoleteCatalogClaim:
            continue
        except Exception as error:
            logger.exception("Knowledge Catalog rebuild failed.")
            if _mark_catalog_rebuild_failed(resolved, claim, error):
                continue
            return activated


@contextmanager
def lease_current_catalog(kb_dir: Path) -> Iterator[CatalogGenerationLease | None]:
    """Keep the current and one recent immutable generation safe during retrieval."""
    with _lease_catalog_generation(kb_dir, generation_id=None) as lease:
        yield lease


@contextmanager
def lease_catalog_generation(
    kb_dir: Path, generation_id: str
) -> Iterator[CatalogGenerationLease | None]:
    """Keep one snapshot-selected immutable Catalog generation safe during retrieval."""
    if not generation_id:
        yield None
        return
    with _lease_catalog_generation(kb_dir, generation_id=generation_id) as lease:
        yield lease


@contextmanager
def _lease_catalog_generation(
    kb_dir: Path, *, generation_id: str | None
) -> Iterator[CatalogGenerationLease | None]:
    resolved = kb_dir.expanduser().resolve()
    state_dir = desktop_state_dir(resolved)
    lease: CatalogGenerationLease | None = None
    with kb_ingest_lock(state_dir):
        connection = _connect(desktop_state_database_path(resolved))
        try:
            if generation_id is None:
                row = connection.execute(
                    """
                    SELECT state.current_generation_id, generations.source_revision,
                        state.is_stale
                    FROM knowledge_catalog_state AS state
                    JOIN knowledge_catalog_generations AS generations
                        ON generations.generation_id = state.current_generation_id
                    WHERE state.singleton = 1
                    """
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT generations.generation_id, generations.source_revision,
                        CASE WHEN state.current_generation_id = generations.generation_id
                            THEN state.is_stale ELSE 0 END
                    FROM knowledge_catalog_generations AS generations
                    JOIN knowledge_catalog_state AS state ON state.singleton = 1
                    WHERE generations.generation_id = ?
                    """,
                    (generation_id,),
                ).fetchone()
            if row is not None:
                lease = CatalogGenerationLease(str(row[0]), int(row[1]), bool(row[2]))
                lease_key = (resolved, lease.generation_id)
                with _LEASE_LOCK:
                    _GENERATION_LEASES[lease_key] = _GENERATION_LEASES.get(lease_key, 0) + 1
        finally:
            connection.close()
    try:
        yield lease
    finally:
        if lease is not None:
            lease_key = (resolved, lease.generation_id)
            with _LEASE_LOCK:
                remaining = _GENERATION_LEASES[lease_key] - 1
                if remaining:
                    _GENERATION_LEASES[lease_key] = remaining
                else:
                    del _GENERATION_LEASES[lease_key]
            cleanup_catalog_generations(resolved)


def cleanup_catalog_generations(kb_dir: Path) -> None:
    """Keep current plus the newest successful predecessor after readers release."""
    resolved = kb_dir.expanduser().resolve()
    try:
        with kb_ingest_lock(desktop_state_dir(resolved)):
            connection = _connect(desktop_state_database_path(resolved))
            try:
                with connection:
                    _delete_old_generations_in(connection, resolved)
            finally:
                connection.close()
    except (OSError, sqlite3.Error):
        logger.warning("Could not clean old Knowledge Catalog generations.", exc_info=True)


def catalog_rebuild_task_in(connection: sqlite3.Connection) -> dict[str, object] | None:
    """Project the singleton durable rebuild for the Task Center."""
    row = connection.execute(
        """
        SELECT tasks.status, tasks.reason, tasks.requested_source_revision,
            tasks.attempt_count, tasks.error_code, tasks.error_reason, tasks.updated_at,
            tasks.completed_at, state.current_generation_id, state.is_stale,
            current.node_count, current.link_count
        FROM knowledge_catalog_rebuild_tasks AS tasks
        JOIN knowledge_catalog_state AS state ON state.singleton = tasks.singleton
        LEFT JOIN knowledge_catalog_generations AS current
            ON current.generation_id = state.current_generation_id
        WHERE tasks.singleton = 1
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "status": str(row[0]),
        "reason": str(row[1]),
        "requested_source_revision": int(row[2]),
        "attempt_count": int(row[3]),
        "error_code": str(row[4]) if row[4] is not None else None,
        "error_reason": str(row[5]) if row[5] is not None else None,
        "updated_at": str(row[6]),
        "completed_at": str(row[7]) if row[7] is not None else None,
        "current_generation_id": str(row[8]) if row[8] is not None else None,
        "stale_serving": bool(row[9]) and row[8] is not None,
        "node_count": int(row[10]) if row[10] is not None else 0,
        "link_count": int(row[11]) if row[11] is not None else 0,
    }


def _has_pending_work(kb_dir: Path) -> bool:
    connection = _connect(desktop_state_database_path(kb_dir))
    try:
        row = connection.execute(
            "SELECT 1 FROM knowledge_catalog_rebuild_tasks WHERE status = 'pending'"
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _claim_catalog_rebuild(kb_dir: Path) -> _CatalogClaim | None:
    with kb_ingest_lock(desktop_state_dir(kb_dir)):
        connection = _connect(desktop_state_database_path(kb_dir))
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT requested_source_revision
                FROM knowledge_catalog_rebuild_tasks WHERE singleton = 1 AND status = 'pending'
                """
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            token = uuid.uuid4().hex
            cursor = connection.execute(
                """
                UPDATE knowledge_catalog_rebuild_tasks
                SET status = 'running', execution_token = ?, attempt_count = attempt_count + 1,
                    error_code = NULL, error_reason = NULL, updated_at = ?, completed_at = NULL
                WHERE singleton = 1 AND status = 'pending'
                    AND requested_source_revision = ?
                """,
                (token, _timestamp(), int(row[0])),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            return _CatalogClaim(token, int(row[0]))
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _build_claim_snapshot(kb_dir: Path, claim: _CatalogClaim) -> CatalogSnapshot:
    connection = _connect(desktop_state_database_path(kb_dir))
    try:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT source_revision FROM knowledge_catalog_state WHERE singleton = 1"
        ).fetchone()
        if row is None or int(row[0]) != claim.source_revision:
            raise _ObsoleteCatalogClaim
        snapshot = build_catalog_snapshot_in(connection, claim.source_revision)
        connection.commit()
        return snapshot
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _publish_claim_snapshot(kb_dir: Path, claim: _CatalogClaim, snapshot: CatalogSnapshot) -> bool:
    with kb_ingest_lock(desktop_state_dir(kb_dir)):
        connection = _connect(desktop_state_database_path(kb_dir))
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT tasks.status, tasks.execution_token, tasks.requested_source_revision,
                    state.source_revision
                FROM knowledge_catalog_rebuild_tasks AS tasks
                JOIN knowledge_catalog_state AS state ON state.singleton = tasks.singleton
                WHERE tasks.singleton = 1
                """
            ).fetchone()
            expected = (
                "running",
                claim.execution_token,
                claim.source_revision,
                claim.source_revision,
            )
            if current != expected:
                connection.rollback()
                raise _ObsoleteCatalogClaim
            _persist_snapshot_in(connection, kb_dir, snapshot)
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _persist_snapshot_in(
    connection: sqlite3.Connection, kb_dir: Path, snapshot: CatalogSnapshot
) -> None:
    generation_id = snapshot.generation_id
    legacy_links = _legacy_endpoint_links(snapshot.links)
    existing = connection.execute(
        "SELECT snapshot_digest FROM knowledge_catalog_generations WHERE generation_id = ?",
        (generation_id,),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO knowledge_catalog_generations (
                generation_id, source_revision, snapshot_digest, status,
                node_count, link_count, created_at
            ) VALUES (?, ?, ?, 'current', ?, ?, ?)
            """,
            (
                generation_id,
                snapshot.source_revision,
                snapshot.snapshot_digest,
                len(snapshot.nodes),
                len(snapshot.links),
                _timestamp(),
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_catalog_nodes (
                generation_id, node_id, parent_node_id, node_order, depth, kind,
                authority, authority_id, title, normalized_title, search_text,
                lifecycle_state, availability, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    generation_id,
                    node.node_id,
                    node.parent_node_id,
                    node.order,
                    node.depth,
                    node.kind,
                    node.authority,
                    node.authority_id,
                    node.title,
                    node.normalized_title,
                    node.search_text,
                    node.lifecycle_state,
                    node.availability,
                    node.metadata_json,
                )
                for node in snapshot.nodes
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_catalog_node_sources (
                generation_id, node_id, source_id, evidence_id, document_id,
                availability, association_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    generation_id,
                    source.node_id,
                    source.source_id,
                    source.evidence_id,
                    source.document_id,
                    source.availability,
                    source.order,
                )
                for source in snapshot.sources
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_catalog_links (
                generation_id, from_node_id, to_node_id, weight
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (generation_id, link.from_node_id, link.to_node_id, link.weight)
                for link in legacy_links
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_catalog_relationships (
                generation_id, source_node_id, target_node_id, relation_kind,
                source_route, target_route, provenance, lifecycle_eligible, weight
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    generation_id,
                    link.from_node_id,
                    link.to_node_id,
                    link.relation_kind,
                    link.source_route,
                    link.target_route,
                    link.provenance,
                    int(link.lifecycle_eligible),
                    link.weight,
                )
                for link in snapshot.links
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_catalog_relationship_sources (
                generation_id, source_node_id, target_node_id, relation_kind,
                binding_role, source_id, evidence_id, document_id, availability
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    generation_id,
                    link.from_node_id,
                    link.to_node_id,
                    link.relation_kind,
                    source.binding_role,
                    source.source_id,
                    source.evidence_id,
                    source.document_id,
                    source.availability,
                )
                for link in snapshot.links
                for source in link.source_bindings
            ),
        )
    elif str(existing[0]) != snapshot.snapshot_digest:
        raise RuntimeError("Knowledge Catalog generation identity collision.")
    previous = connection.execute(
        "SELECT current_generation_id FROM knowledge_catalog_state WHERE singleton = 1"
    ).fetchone()
    if previous is not None and previous[0] is not None and str(previous[0]) != generation_id:
        connection.execute(
            "UPDATE knowledge_catalog_generations SET status = 'recent' WHERE generation_id = ?",
            (str(previous[0]),),
        )
    connection.execute(
        "UPDATE knowledge_catalog_generations SET status = 'current' WHERE generation_id = ?",
        (generation_id,),
    )
    now = _timestamp()
    connection.execute(
        """
        UPDATE knowledge_catalog_state
        SET current_generation_id = ?, is_stale = 0, stale_since = NULL, activated_at = ?
        WHERE singleton = 1 AND source_revision = ?
        """,
        (generation_id, now, snapshot.source_revision),
    )
    connection.execute(
        """
        UPDATE knowledge_catalog_rebuild_tasks
        SET status = 'completed', execution_token = NULL, error_code = NULL,
            error_reason = NULL, updated_at = ?, completed_at = ?
        WHERE singleton = 1
        """,
        (now, now),
    )
    _delete_old_generations_in(connection, kb_dir)


def _legacy_endpoint_links(links: tuple[CatalogLink, ...]) -> tuple[CatalogLink, ...]:
    """Project typed relationships onto the legacy endpoint-only link table."""
    selected: dict[tuple[str, str], CatalogLink] = {}
    for link in links:
        key = (link.from_node_id, link.to_node_id)
        current = selected.get(key)
        if current is None or link.weight > current.weight:
            selected[key] = link
    return tuple(selected[key] for key in sorted(selected))


def _delete_old_generations_in(connection: sqlite3.Connection, kb_dir: Path) -> None:
    recent = tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT generation_id FROM knowledge_catalog_generations
            WHERE status = 'recent' ORDER BY created_at DESC, generation_id DESC
            """
        ).fetchall()
    )
    with _LEASE_LOCK:
        deletable = tuple(
            generation_id
            for generation_id in recent[1:]
            if (kb_dir, generation_id) not in _GENERATION_LEASES
        )
    connection.executemany(
        "DELETE FROM knowledge_catalog_generations WHERE generation_id = ?",
        ((generation_id,) for generation_id in deletable),
    )


def _mark_catalog_rebuild_failed(kb_dir: Path, claim: _CatalogClaim, error: Exception) -> bool:
    """Persist failure and return whether one bounded automatic retry was queued."""
    try:
        with kb_ingest_lock(desktop_state_dir(kb_dir)):
            connection = _connect(desktop_state_database_path(kb_dir))
            try:
                with connection:
                    row = connection.execute(
                        """
                        SELECT attempt_count FROM knowledge_catalog_rebuild_tasks
                        WHERE singleton = 1 AND status = 'running' AND execution_token = ?
                        """,
                        (claim.execution_token,),
                    ).fetchone()
                    if row is None:
                        return False
                    retry = int(row[0]) < _MAX_CATALOG_BUILD_ATTEMPTS
                    now = _timestamp()
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_catalog_rebuild_tasks
                        SET status = ?, execution_token = NULL, error_code = ?,
                            error_reason = ?, updated_at = ?, completed_at = ?
                        WHERE singleton = 1 AND status = 'running' AND execution_token = ?
                        """,
                        (
                            "pending" if retry else "failed",
                            CATALOG_BUILD_ERROR_CODE,
                            str(error)[:1000],
                            now,
                            None if retry else now,
                            claim.execution_token,
                        ),
                    )
                    return retry and cursor.rowcount == 1
            finally:
                connection.close()
    except (OSError, sqlite3.Error):
        logger.warning("Could not persist Knowledge Catalog rebuild failure.", exc_info=True)
    return False


def _run_catalog_worker(kb_dir: Path) -> None:
    try:
        while True:
            rebuild_pending_catalog(kb_dir)
            with _WORKER_LOCK:
                recover = kb_dir in _RECOVERY_REQUESTS
                _RECOVERY_REQUESTS.discard(kb_dir)
                if kb_dir in _RERUN_REQUESTS:
                    _RERUN_REQUESTS.discard(kb_dir)
                    rerun = True
                else:
                    rerun = False
                if not recover and not rerun:
                    _ACTIVE_WORKERS.discard(kb_dir)
                    return
            if recover:
                recover_catalog_rebuilds(kb_dir)
    except Exception:
        logger.exception("Knowledge Catalog rebuild worker stopped unexpectedly.")
    finally:
        with _WORKER_LOCK:
            _ACTIVE_WORKERS.discard(kb_dir)
            _RERUN_REQUESTS.discard(kb_dir)
            _RECOVERY_REQUESTS.discard(kb_dir)


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = connect_database(database_path)
    return connection
