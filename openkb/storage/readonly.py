"""Read-only SQLite connections for Desktop reporting and evaluation."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_desktop_read_only(database_path: Path) -> sqlite3.Connection:
    """Open an existing Desktop state database without creating or mutating it."""
    resolved = database_path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError("Desktop retrieval evaluation requires an open knowledge base.")
    try:
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as error:
        raise ValueError("Desktop retrieval evaluation database is unavailable.") from error
