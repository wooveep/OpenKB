"""SQLite-backed Desktop Knowledge Base creation and runtime binding.

The Desktop Runtime owns one active knowledge base at a time.  This module is
deliberately narrow: it establishes the new on-disk format and checkpointing
boundary that later import and retrieval work can build on, without teaching
the Tauri shell about SQLite or legacy workspace files.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_workspace_migrations import (
    MODEL_CALL_MIGRATION_STATEMENTS,
    RAW_ASSET_INTEGRITY_MIGRATION_STATEMENTS,
    RECOVERY_RUN_MIGRATION_STATEMENTS,
    SOURCE_IMAGE_MIGRATION_STATEMENTS,
)
from openkb.locks import kb_ingest_lock

_STATE_DIRNAME = ".openkb"
_STATE_FILENAME = "state.sqlite3"
_INITIALIZING_FILENAME = "initializing"
_DESKTOP_FORMAT = "openkb-desktop"


class DesktopKnowledgeBaseError(RuntimeError):
    """A stable domain error for Desktop Knowledge Base operations."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DesktopKnowledgeBaseNotFoundError(DesktopKnowledgeBaseError):
    """Raised when a selected directory is not a Desktop Knowledge Base."""

    def __init__(self, kb_dir: Path) -> None:
        super().__init__(
            "desktop_knowledge_base_not_found", f"Not a Desktop Knowledge Base: {kb_dir}"
        )


class LegacyKnowledgeBaseUnsupportedError(DesktopKnowledgeBaseError):
    """Raised when an old CLI/Web workspace is selected in Desktop."""

    def __init__(self, kb_dir: Path) -> None:
        super().__init__(
            "legacy_knowledge_base_unsupported",
            f"Legacy Knowledge Bases cannot be opened by OpenKB Desktop: {kb_dir}",
        )


class DesktopKnowledgeBaseAlreadyExistsError(DesktopKnowledgeBaseError):
    """Raised when Create targets an existing Desktop Knowledge Base."""

    def __init__(self, kb_dir: Path) -> None:
        super().__init__(
            "desktop_knowledge_base_already_exists",
            f"A Desktop Knowledge Base already exists at: {kb_dir}",
        )


class DesktopKnowledgeBaseDirectoryNotEmptyError(DesktopKnowledgeBaseError):
    """Raised when Create would mix a new knowledge base into user files."""

    def __init__(self, kb_dir: Path) -> None:
        super().__init__(
            "desktop_knowledge_base_directory_not_empty",
            f"Choose an empty directory for the Desktop Knowledge Base: {kb_dir}",
        )


class DesktopKnowledgeBaseStateError(DesktopKnowledgeBaseError):
    """Raised when the SQLite authority database is invalid or incompatible."""

    def __init__(self, message: str) -> None:
        super().__init__("desktop_knowledge_base_state_invalid", message)


class DesktopKnowledgeBaseMigrationError(DesktopKnowledgeBaseError):
    """Raised when a database requires a newer Desktop release."""

    def __init__(self, version: int) -> None:
        super().__init__(
            "desktop_knowledge_base_schema_too_new",
            "This Desktop Knowledge Base uses schema version "
            f"{version}, which is newer than this OpenKB Desktop release.",
        )


@dataclass(frozen=True)
class DesktopKnowledgeBase:
    """The small authority record visible while a knowledge base is active."""

    kb_dir: str
    name: str
    schema_version: int
    last_checkpoint_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "kb_dir": self.kb_dir,
            "name": self.name,
            "schema_version": self.schema_version,
            "last_checkpoint_at": self.last_checkpoint_at,
        }


@dataclass(frozen=True)
class DesktopKnowledgeBaseActivation:
    """One completed create/open activation with a durable switch checkpoint."""

    knowledge_base: DesktopKnowledgeBase
    previous_kb_dir: str | None
    checkpointed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "knowledge_base": self.knowledge_base.as_dict(),
            "events": [
                {
                    "kind": "knowledge_base.activated",
                    "data": {
                        "kb_dir": self.knowledge_base.kb_dir,
                        "name": self.knowledge_base.name,
                        "previous_kb_dir": self.previous_kb_dir,
                        "checkpointed": self.checkpointed,
                    },
                }
            ],
        }


# Each migration is intentionally additive and recorded before a newer command
# can use its tables.  Later tickets append entries here rather than modifying
# an already shipped migration.
_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY CHECK(version > 0),
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE runtime_checkpoints (
                checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE raw_assets (
                asset_sha256 TEXT PRIMARY KEY,
                byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                media_type TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE source_documents (
                document_id TEXT PRIMARY KEY,
                asset_sha256 TEXT NOT NULL UNIQUE
                    REFERENCES raw_assets(asset_sha256),
                display_name TEXT NOT NULL,
                source_format TEXT NOT NULL,
                availability TEXT NOT NULL
                    CHECK(availability IN ('available', 'failed')),
                created_at TEXT NOT NULL,
                available_at TEXT
            )
            """,
            """
            CREATE TABLE document_ir_blocks (
                block_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES source_documents(document_id)
                    ON DELETE CASCADE,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                heading_path TEXT NOT NULL,
                locator_json TEXT NOT NULL,
                UNIQUE(document_id, ordinal)
            )
            """,
            """
            CREATE TABLE evidence_refs (
                evidence_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES source_documents(document_id)
                    ON DELETE CASCADE,
                block_id TEXT NOT NULL REFERENCES document_ir_blocks(block_id)
                    ON DELETE CASCADE,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                text TEXT NOT NULL,
                locator_json TEXT NOT NULL,
                UNIQUE(document_id, ordinal)
            )
            """,
            """
            CREATE VIRTUAL TABLE evidence_fts USING fts5(
                evidence_id UNINDEXED,
                document_id UNINDEXED,
                content,
                tokenize = 'unicode61'
            )
            """,
            """
            CREATE TABLE import_jobs (
                job_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                document_id TEXT,
                status TEXT NOT NULL
                    CHECK(status IN ('running', 'completed', 'failed')),
                progress INTEGER NOT NULL CHECK(progress BETWEEN 0 AND 100),
                error_code TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """,
            """
            CREATE TABLE stage_runs (
                stage_run_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
                progress INTEGER NOT NULL CHECK(progress BETWEEN 0 AND 100),
                error_code TEXT,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(job_id, stage)
            )
            """,
            """
            CREATE INDEX source_documents_available_idx
                ON source_documents(availability, created_at DESC)
            """,
            """
            CREATE INDEX import_jobs_status_idx
                ON import_jobs(status, created_at DESC)
            """,
        ),
    ),
    (
        3,
        (
            """
            CREATE TABLE import_job_runtime (
                job_id TEXT PRIMARY KEY REFERENCES import_jobs(job_id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN (
                    'running', 'paused', 'cancelled', 'recoverable', 'completed', 'failed'
                )),
                lease_owner TEXT,
                lease_expires_at TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            INSERT INTO import_job_runtime (
                job_id, status, lease_owner, lease_expires_at, updated_at
            )
            SELECT job_id, status, NULL, NULL, created_at
            FROM import_jobs
            """,
            """
            CREATE TABLE stage_run_runtime (
                stage_run_id TEXT PRIMARY KEY REFERENCES stage_runs(stage_run_id) ON DELETE CASCADE,
                job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN (
                    'pending', 'running', 'paused', 'cancelled', 'completed', 'failed', 'skipped'
                )),
                checkpoint_json TEXT,
                error_code TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            INSERT INTO stage_run_runtime (
                stage_run_id, job_id, status, checkpoint_json, error_code, updated_at
            )
            SELECT stage_run_id, job_id, status, NULL, error_code,
                COALESCE(completed_at, started_at, '')
            FROM stage_runs
            """,
            """
            UPDATE import_jobs
            SET progress = 0, error_code = NULL
            WHERE status = 'running'
            """,
            """
            UPDATE stage_runs
            SET status = 'pending', progress = 0, error_code = NULL,
                started_at = NULL, completed_at = NULL
            WHERE job_id IN (SELECT job_id FROM import_jobs WHERE status = 'running')
            """,
            """
            UPDATE stage_run_runtime
            SET status = 'pending', checkpoint_json = NULL, error_code = NULL
            WHERE job_id IN (SELECT job_id FROM import_jobs WHERE status = 'running')
            """,
            """
            CREATE INDEX import_job_runtime_status_idx
                ON import_job_runtime(status, updated_at DESC)
            """,
            """
            CREATE INDEX stage_run_runtime_job_idx
                ON stage_run_runtime(job_id, updated_at DESC)
            """,
        ),
    ),
    (4, MODEL_CALL_MIGRATION_STATEMENTS),
    (5, RECOVERY_RUN_MIGRATION_STATEMENTS),
    (6, RAW_ASSET_INTEGRITY_MIGRATION_STATEMENTS),
    (7, SOURCE_IMAGE_MIGRATION_STATEMENTS),
)


class DesktopKnowledgeBaseRuntime:
    """Own the one active Desktop Knowledge Base for one Python Engine."""

    def __init__(self) -> None:
        self._active: DesktopKnowledgeBase | None = None
        self._lock = threading.RLock()

    def create(self, kb_dir: Path, *, name: str | None = None) -> DesktopKnowledgeBaseActivation:
        """Create a new SQLite-authoritative Desktop Knowledge Base and activate it."""
        resolved = _resolve_directory(kb_dir)
        if resolved.exists() and not resolved.is_dir():
            raise DesktopKnowledgeBaseDirectoryNotEmptyError(resolved)
        if _is_legacy_knowledge_base(resolved) and not _state_database_path(resolved).exists():
            raise LegacyKnowledgeBaseUnsupportedError(resolved)

        try:
            resolved.mkdir(parents=True, exist_ok=True)
            state_dir = _state_dir(resolved)
            display_name = _display_name(name, resolved)
            with kb_ingest_lock(state_dir):
                _recover_interrupted_initialization(resolved)
                _require_creatable_knowledge_base(resolved)
                database_path = _state_database_path(resolved)
                raw_dir = resolved / "raw"
                raw_dir.mkdir(exist_ok=True)
                initialization_marker = _initialization_marker_path(resolved)
                initialization_marker.touch(exist_ok=False)
                try:
                    connection: sqlite3.Connection | None = None
                    try:
                        connection = _connect(database_path)
                        connection.execute("BEGIN IMMEDIATE")
                        _apply_migrations(connection, creating=True, in_transaction=True)
                        _set_metadata(connection, "format", _DESKTOP_FORMAT, in_transaction=True)
                        _set_metadata(
                            connection,
                            "knowledge_base_name",
                            display_name,
                            in_transaction=True,
                        )
                        connection.commit()
                    except BaseException:
                        if connection is not None:
                            connection.rollback()
                        raise
                    finally:
                        if connection is not None:
                            connection.close()
                    initialization_marker.unlink()
                except BaseException:
                    _remove_initial_database(database_path)
                    initialization_marker.unlink(missing_ok=True)
                    raise
        except DesktopKnowledgeBaseError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise DesktopKnowledgeBaseStateError(
                f"Could not create Desktop Knowledge Base at {resolved}: {error}"
            ) from error

        return self._activate(_load_desktop_knowledge_base(resolved))

    def open(self, kb_dir: Path) -> DesktopKnowledgeBaseActivation:
        """Open an existing new-format Desktop Knowledge Base and activate it."""
        return self._activate(_load_desktop_knowledge_base(_resolve_directory(kb_dir)))

    def active(self) -> DesktopKnowledgeBase | None:
        """Return the only knowledge base currently bound to this runtime."""
        with self._lock:
            return self._active

    def _activate(self, target: DesktopKnowledgeBase) -> DesktopKnowledgeBaseActivation:
        with self._lock:
            previous = self._active
            checkpointed = False
            if previous is not None and previous.kb_dir != target.kb_dir:
                _write_checkpoint(Path(previous.kb_dir), reason="knowledge_base_switched")
                checkpointed = True
            self._active = target
            return DesktopKnowledgeBaseActivation(
                knowledge_base=target,
                previous_kb_dir=previous.kb_dir if previous is not None else None,
                checkpointed=checkpointed,
            )


def _resolve_directory(kb_dir: Path) -> Path:
    return kb_dir.expanduser().resolve()


def _state_dir(kb_dir: Path) -> Path:
    return kb_dir / _STATE_DIRNAME


def _state_database_path(kb_dir: Path) -> Path:
    return _state_dir(kb_dir) / _STATE_FILENAME


def desktop_state_dir(kb_dir: Path) -> Path:
    """Return the Desktop-owned state directory for a known knowledge base."""
    return _state_dir(kb_dir)


def desktop_state_database_path(kb_dir: Path) -> Path:
    """Return the SQLite authority path for a known Desktop knowledge base."""
    return _state_database_path(kb_dir)


def _initialization_marker_path(kb_dir: Path) -> Path:
    return _state_dir(kb_dir) / _INITIALIZING_FILENAME


def _is_legacy_knowledge_base(kb_dir: Path) -> bool:
    state_dir = _state_dir(kb_dir)
    return (state_dir / "hashes.json").is_file() or (kb_dir / "wiki").is_dir()


def _display_name(name: str | None, kb_dir: Path) -> str:
    if name is None:
        return kb_dir.name
    candidate = name.strip()
    return candidate or kb_dir.name


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _require_creatable_knowledge_base(kb_dir: Path) -> None:
    if _state_database_path(kb_dir).exists():
        raise DesktopKnowledgeBaseAlreadyExistsError(kb_dir)
    if _is_legacy_knowledge_base(kb_dir):
        raise LegacyKnowledgeBaseUnsupportedError(kb_dir)

    root_entries = tuple(kb_dir.iterdir())
    if any(entry.name not in {_STATE_DIRNAME, "raw"} for entry in root_entries):
        raise DesktopKnowledgeBaseDirectoryNotEmptyError(kb_dir)

    raw_dir = kb_dir / "raw"
    if raw_dir.exists() and (not raw_dir.is_dir() or any(raw_dir.iterdir())):
        raise DesktopKnowledgeBaseDirectoryNotEmptyError(kb_dir)

    state_dir = _state_dir(kb_dir)
    if not state_dir.is_dir():
        raise DesktopKnowledgeBaseDirectoryNotEmptyError(kb_dir)
    if any(entry.name != "ingest.lock" for entry in state_dir.iterdir()):
        raise DesktopKnowledgeBaseDirectoryNotEmptyError(kb_dir)


def _recover_interrupted_initialization(kb_dir: Path) -> None:
    marker_path = _initialization_marker_path(kb_dir)
    if not marker_path.exists():
        return

    database_path = _state_database_path(kb_dir)
    if _is_completed_initial_database(database_path):
        marker_path.unlink()
        return

    _remove_initial_database(database_path)
    marker_path.unlink()


def _is_completed_initial_database(database_path: Path) -> bool:
    if not database_path.is_file():
        return False

    try:
        connection = _connect(database_path)
        try:
            has_initial_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 1"
            ).fetchone()
            return (
                has_initial_migration is not None
                and _metadata(connection, "format") == _DESKTOP_FORMAT
                and bool(_metadata(connection, "knowledge_base_name"))
            )
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def _remove_initial_database(database_path: Path) -> None:
    for path in (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
    ):
        path.unlink(missing_ok=True)


def _apply_migrations(
    connection: sqlite3.Connection, *, creating: bool, in_transaction: bool = False
) -> int:
    if creating:
        applied: set[int] = set()
    else:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if table is None:
            raise DesktopKnowledgeBaseStateError(
                "Desktop Knowledge Base is missing its schema migration ledger."
            )
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {int(row[0]) for row in rows}

    latest = _MIGRATIONS[-1][0]
    if applied and max(applied) > latest:
        raise DesktopKnowledgeBaseMigrationError(max(applied))
    expected_applied = set(range(1, max(applied, default=0) + 1))
    if applied != expected_applied:
        raise DesktopKnowledgeBaseStateError(
            "Desktop Knowledge Base has a non-contiguous migration ledger."
        )

    for version, statements in _MIGRATIONS:
        if version in applied:
            continue
        now = _timestamp()
        if in_transaction:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, now),
            )
        else:
            with connection:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, now),
                )
        applied.add(version)
    return latest


def _recover_interrupted_import_jobs(connection: sqlite3.Connection) -> None:
    """Converge pre-publication import work after an Engine or Shell crash.

    Documents and retrieval artifacts are published in their own final
    transaction, so a running job cannot own a partially available document.
    The caller holds the KB ingest lock; a live worker keeps that lock from job
    creation through publication. An expired lease is recovered as such; an
    unexpired lease whose lock is now available is also abandoned work. Retaining
    completed stage checkpoints turns either case into a recoverable task.
    """
    interrupted = connection.execute(
        "SELECT job_id, lease_expires_at FROM import_job_runtime WHERE status = 'running'"
    ).fetchall()
    if not interrupted:
        return

    now = _timestamp()
    with connection:
        for job_id, lease_expires_at in interrupted:
            error_code = (
                "import_lease_expired"
                if _lease_expired(lease_expires_at, now)
                else "import_interrupted"
            )
            connection.execute(
                """
                UPDATE stage_run_runtime
                SET status = CASE WHEN status = 'running' THEN 'paused' ELSE status END,
                    error_code = CASE WHEN status = 'running' THEN ? ELSE error_code END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (error_code, now, job_id),
            )
            connection.execute(
                """
                UPDATE import_job_runtime
                SET status = 'recoverable', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )


def _lease_expired(lease_expires_at: object, now: str) -> bool:
    if not isinstance(lease_expires_at, str):
        return True
    try:
        return dt.datetime.fromisoformat(lease_expires_at) <= dt.datetime.fromisoformat(now)
    except (TypeError, ValueError):
        return True


def _set_metadata(
    connection: sqlite3.Connection, key: str, value: str, *, in_transaction: bool = False
) -> None:
    if in_transaction:
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    else:
        with connection:
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def _metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row is not None else None


def _load_desktop_knowledge_base(kb_dir: Path) -> DesktopKnowledgeBase:
    if not kb_dir.is_dir():
        raise DesktopKnowledgeBaseNotFoundError(kb_dir)
    database_path = _state_database_path(kb_dir)
    initialization_marker = _initialization_marker_path(kb_dir)
    if _is_legacy_knowledge_base(kb_dir) and not database_path.exists():
        raise LegacyKnowledgeBaseUnsupportedError(kb_dir)
    if not database_path.is_file() and not initialization_marker.exists():
        raise DesktopKnowledgeBaseNotFoundError(kb_dir)

    try:
        with kb_ingest_lock(_state_dir(kb_dir)):
            _recover_interrupted_initialization(kb_dir)
            if _is_legacy_knowledge_base(kb_dir) and not database_path.exists():
                raise LegacyKnowledgeBaseUnsupportedError(kb_dir)
            if not database_path.is_file():
                raise DesktopKnowledgeBaseNotFoundError(kb_dir)
            connection = _connect(database_path)
            try:
                schema_version = _apply_migrations(connection, creating=False)
                if _metadata(connection, "format") != _DESKTOP_FORMAT:
                    raise DesktopKnowledgeBaseStateError(
                        "Selected SQLite database is not an OpenKB Desktop Knowledge Base."
                    )
                name = _metadata(connection, "knowledge_base_name")
                if not name:
                    raise DesktopKnowledgeBaseStateError(
                        "Desktop Knowledge Base is missing its display name."
                    )
                _recover_interrupted_import_jobs(connection)
                row = connection.execute(
                    "SELECT created_at FROM runtime_checkpoints ORDER BY checkpoint_id DESC LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
    except DesktopKnowledgeBaseError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise DesktopKnowledgeBaseStateError(
            f"Could not open Desktop Knowledge Base at {kb_dir}: {error}"
        ) from error

    return DesktopKnowledgeBase(
        kb_dir=str(kb_dir),
        name=name,
        schema_version=schema_version,
        last_checkpoint_at=str(row[0]) if row is not None else None,
    )


def _write_checkpoint(kb_dir: Path, *, reason: str) -> None:
    database_path = _state_database_path(kb_dir)
    try:
        with kb_ingest_lock(_state_dir(kb_dir)):
            connection = _connect(database_path)
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO runtime_checkpoints (reason, created_at) VALUES (?, ?)",
                        (reason, _timestamp()),
                    )
            finally:
                connection.close()
    except (OSError, sqlite3.Error) as error:
        raise DesktopKnowledgeBaseStateError(
            f"Could not checkpoint Desktop Knowledge Base at {kb_dir}: {error}"
        ) from error


def _timestamp() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()
