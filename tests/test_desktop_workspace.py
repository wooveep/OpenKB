"""Behavior checks for Desktop Knowledge Base creation and active binding."""

from __future__ import annotations

import sqlite3

import pytest

from openkb.desktop_workspace import (
    DesktopKnowledgeBaseRuntime,
    LegacyKnowledgeBaseUnsupportedError,
)


def test_create_open_and_switch_desktop_knowledge_bases_checkpoint_the_previous_one(tmp_path):
    """One Engine owns one active SQLite knowledge base and checkpoints before a switch."""
    runtime = DesktopKnowledgeBaseRuntime()
    first_dir = tmp_path / "first"
    first = runtime.create(first_dir, name="First knowledge base")

    assert first.knowledge_base.name == "First knowledge base"
    assert first.knowledge_base.schema_version == 1
    assert first.knowledge_base.last_checkpoint_at is None
    assert (first_dir / "raw").is_dir()
    database_path = first_dir / ".openkb" / "state.sqlite3"
    assert database_path.is_file()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert connection.execute("SELECT value FROM metadata WHERE key = 'format'").fetchone() == (
            "openkb-desktop",
        )

    second_dir = tmp_path / "second"
    second = runtime.create(second_dir)

    assert second.previous_kb_dir == str(first_dir)
    assert second.checkpointed is True
    assert runtime.active() == second.knowledge_base
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT reason FROM runtime_checkpoints").fetchall() == [
            ("knowledge_base_switched",)
        ]

    reopened = DesktopKnowledgeBaseRuntime().open(first_dir)
    assert reopened.knowledge_base.name == "First knowledge base"
    assert reopened.knowledge_base.last_checkpoint_at is not None


def test_opening_a_legacy_knowledge_base_is_rejected_without_creating_desktop_state(kb_dir):
    """Desktop does not migrate or change an existing CLI/Web knowledge base."""
    with pytest.raises(LegacyKnowledgeBaseUnsupportedError) as error:
        DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert error.value.code == "legacy_knowledge_base_unsupported"
    assert not (kb_dir / ".openkb" / "state.sqlite3").exists()
