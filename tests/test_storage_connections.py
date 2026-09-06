"""Connection policy must preserve constraints and caller-owned transactions."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest

from openkb.shared.clock import timestamp
from openkb.storage.readonly import connect_desktop_read_only
from openkb.storage.sqlite import connect_database


def test_state_connection_enforces_foreign_keys_and_caller_rollback(tmp_path):
    path = tmp_path / "state.sqlite3"
    with closing(connect_database(path, timeout=0.125)) as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (125,)
        connection.executescript(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id));"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child VALUES (99)")
        connection.rollback()
        connection.execute("INSERT INTO parent VALUES (1)")
        connection.execute("INSERT INTO child VALUES (1)")
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM parent").fetchone() == (0,)
        with connection:
            connection.execute("INSERT INTO parent VALUES (2)")
    with closing(connect_desktop_read_only(path)) as connection:
        assert connection.execute("SELECT id FROM parent").fetchall() == [(2,)]
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM parent")


def test_read_only_connection_never_creates_a_missing_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError):
        connect_desktop_read_only(path)
    assert not path.exists()


def test_timestamp_retains_explicit_utc_offset():
    value = timestamp()
    assert value.endswith("+00:00")
    assert datetime.fromisoformat(value).utcoffset() == timedelta(0)
