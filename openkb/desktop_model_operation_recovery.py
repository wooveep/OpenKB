"""Engine-activation recovery for ephemeral model-operation authority."""

from __future__ import annotations

import sqlite3


def discard_model_operation_retry_permits_in(connection: sqlite3.Connection) -> int:
    """Discard retry authority that cannot survive an Engine activation."""
    cursor = connection.execute("DELETE FROM model_operation_retry_permits")
    return cursor.rowcount
