"""Cross-platform regression coverage for Desktop migration backups."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.workspace import backup as desktop_workspace_backup


class _TrackedValidationConnection:
    """Model sqlite3's transaction context without implicitly closing it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.closed = False

    def __enter__(self) -> _TrackedValidationConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return bool(self._connection.__exit__(*exc_info))

    def execute(self, sql: str) -> sqlite3.Cursor:
        return self._connection.execute(sql)

    def close(self) -> None:
        self.closed = True
        self._connection.close()


def test_migration_backup_closes_validation_connection_before_windows_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Windows rejects replacing a migration backup that this process still has open."""
    openkb_dir = tmp_path / ".openkb"
    openkb_dir.mkdir()
    database_path = openkb_dir / "state.sqlite3"
    source = sqlite3.connect(database_path)
    source.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    source.execute("INSERT INTO schema_migrations (version) VALUES (44)")
    source.commit()

    real_connect = sqlite3.connect
    real_replace = Path.replace
    validation_connections: list[_TrackedValidationConnection] = []
    connect_count = 0

    def tracked_connect(path: Path) -> sqlite3.Connection | _TrackedValidationConnection:
        nonlocal connect_count
        connect_count += 1
        connection = real_connect(path)
        if connect_count == 1:
            return connection
        tracked = _TrackedValidationConnection(connection)
        validation_connections.append(tracked)
        return tracked

    def windows_replace(path: Path, target: Path) -> Path:
        if any(not connection.closed for connection in validation_connections):
            raise PermissionError(32, "The process cannot access the file", str(path))
        return real_replace(path, target)

    monkeypatch.setattr(desktop_workspace_backup.sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(Path, "replace", windows_replace)

    try:
        backup = desktop_workspace_backup.create_migration_backup(
            source,
            database_path=database_path,
            current_version=44,
            target_version=51,
        )
    finally:
        source.close()
        for connection in validation_connections:
            if not connection.closed:
                connection.close()

    assert backup.is_file()
    assert validation_connections
    assert all(connection.closed for connection in validation_connections)
