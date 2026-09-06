"""Connection policy shared by the owners of Desktop SQLite state.

Callers own the ingest lock, transaction scope, and connection lifetime. This
factory does not migrate schemas, commit writes, or change journal settings.
Read-only reporting and backup connections retain their separate policies.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_database(database_path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open a state connection with foreign-key enforcement enabled."""
    connection = sqlite3.connect(database_path, timeout=timeout)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        connection.close()
        raise
    return connection
