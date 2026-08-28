"""Provider-free backup boundary for atomic Desktop schema migrations."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Protocol

_MAX_BACKUPS_PER_MIGRATION_EDGE = 3


class DesktopMigrationApplier(Protocol):
    def __call__(
        self,
        connection: sqlite3.Connection,
        *,
        creating: bool,
        in_transaction: bool = False,
    ) -> int: ...


def migrate_existing_database(
    connection: sqlite3.Connection,
    *,
    database_path: Path,
    latest_version: int,
    apply_migrations: DesktopMigrationApplier,
) -> int:
    """Back up an older authority database, then migrate it in one transaction."""
    row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    current = int(row[0]) if row is not None and row[0] is not None else 0
    if current >= latest_version:
        return apply_migrations(connection, creating=False)
    create_migration_backup(
        connection,
        database_path=database_path,
        current_version=current,
        target_version=latest_version,
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        schema_version = apply_migrations(
            connection,
            creating=False,
            in_transaction=True,
        )
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    return schema_version


def create_migration_backup(
    connection: sqlite3.Connection,
    *,
    database_path: Path,
    current_version: int,
    target_version: int,
) -> Path:
    """Create one validated backup of the source state for this migration attempt."""
    backup_dir = database_path.parent / "migration-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (
        f"state-v{current_version}-before-v{target_version}-{uuid.uuid4().hex}.sqlite3"
    )
    temporary = backup_dir / f".{backup_path.name}.{uuid.uuid4().hex}.tmp"
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(temporary)
        connection.backup(destination)
        destination.close()
        destination = None
        if not valid_migration_backup(temporary, expected_version=current_version):
            raise sqlite3.DatabaseError("Migration backup integrity check failed.")
        temporary.replace(backup_path)
        _prune_migration_backups(
            backup_dir,
            current_version=current_version,
            target_version=target_version,
        )
        return backup_path
    finally:
        if destination is not None:
            destination.close()
        temporary.unlink(missing_ok=True)


def valid_migration_backup(path: Path, *, expected_version: int) -> bool:
    """Accept only an intact backup with the expected migration ledger head."""
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path) as backup:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            version = backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return integrity == ("ok",) and version == (expected_version,)
    except sqlite3.Error:
        return False


def _prune_migration_backups(
    backup_dir: Path,
    *,
    current_version: int,
    target_version: int,
) -> None:
    pattern = f"state-v{current_version}-before-v{target_version}-*.sqlite3"
    backups = sorted(
        (path for path in backup_dir.glob(pattern) if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for obsolete in backups[_MAX_BACKUPS_PER_MIGRATION_EDGE:]:
        obsolete.unlink(missing_ok=True)
