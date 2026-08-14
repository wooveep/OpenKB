"""Behavior checks for Desktop Knowledge Base creation and active binding."""

from __future__ import annotations

import sqlite3

import pytest

from openkb import desktop_workspace
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseNotFoundError,
    DesktopKnowledgeBaseRuntime,
    DesktopKnowledgeBaseStateError,
    LegacyKnowledgeBaseUnsupportedError,
)


def test_create_open_and_switch_desktop_knowledge_bases_checkpoint_the_previous_one(tmp_path):
    """One Engine owns one active SQLite knowledge base and checkpoints before a switch."""
    runtime = DesktopKnowledgeBaseRuntime()
    first_dir = tmp_path / "first"
    first = runtime.create(first_dir, name="First knowledge base")

    assert first.knowledge_base.name == "First knowledge base"
    assert first.knowledge_base.schema_version == 2
    assert first.knowledge_base.last_checkpoint_at is None
    assert (first_dir / "raw").is_dir()
    database_path = first_dir / ".openkb" / "state.sqlite3"
    assert database_path.is_file()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (1,),
            (2,),
        ]
        assert connection.execute("SELECT value FROM metadata WHERE key = 'format'").fetchone() == (
            "openkb-desktop",
        )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'knowledge_base_name'"
        ).fetchone() == ("First knowledge base",)

    second_dir = tmp_path / "second"
    second = runtime.create(second_dir)

    assert second.previous_kb_dir == str(first_dir)
    assert second.checkpointed is True
    assert runtime.active() == second.knowledge_base
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT reason FROM runtime_checkpoints").fetchall() == [
            ("knowledge_base_switched",)
        ]

    (first_dir / "wiki").mkdir()
    reopened = DesktopKnowledgeBaseRuntime().open(first_dir)
    assert reopened.knowledge_base.name == "First knowledge base"
    assert reopened.knowledge_base.last_checkpoint_at is not None


def test_opening_a_legacy_knowledge_base_is_rejected_without_creating_desktop_state(kb_dir):
    """Desktop does not migrate or change an existing CLI/Web knowledge base."""
    with pytest.raises(LegacyKnowledgeBaseUnsupportedError) as error:
        DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert error.value.code == "legacy_knowledge_base_unsupported"
    assert not (kb_dir / ".openkb" / "state.sqlite3").exists()
    assert not (kb_dir / ".openkb" / "ingest.lock").exists()


def test_opening_a_plain_directory_does_not_create_desktop_state(tmp_path):
    """Choosing a non-knowledge-base directory leaves it untouched."""
    directory = tmp_path / "plain-directory"
    directory.mkdir()

    with pytest.raises(DesktopKnowledgeBaseNotFoundError):
        DesktopKnowledgeBaseRuntime().open(directory)

    assert not (directory / ".openkb").exists()


def test_failed_initialization_leaves_a_knowledge_base_directory_reusable(tmp_path, monkeypatch):
    """A failed first transaction does not strand a partial Desktop Knowledge Base."""
    kb_dir = tmp_path / "retryable"
    original_set_metadata = desktop_workspace._set_metadata

    def fail_metadata(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated metadata write failure")

    monkeypatch.setattr(desktop_workspace, "_set_metadata", fail_metadata)
    with pytest.raises(DesktopKnowledgeBaseStateError):
        DesktopKnowledgeBaseRuntime().create(kb_dir)

    assert not (kb_dir / ".openkb" / "state.sqlite3").exists()
    assert (kb_dir / "raw").is_dir()
    assert not any((kb_dir / "raw").iterdir())

    monkeypatch.setattr(desktop_workspace, "_set_metadata", original_set_metadata)
    assert DesktopKnowledgeBaseRuntime().create(kb_dir).knowledge_base.name == "retryable"


def test_interrupted_initialization_is_recovered_before_opening_or_recreating(tmp_path):
    """A restart never exposes a SQLite file from an interrupted initial creation."""
    kb_dir = tmp_path / "interrupted"
    state_dir = kb_dir / ".openkb"
    state_dir.mkdir(parents=True)
    (kb_dir / "raw").mkdir()
    (state_dir / "initializing").touch()
    sqlite3.connect(state_dir / "state.sqlite3").close()

    with pytest.raises(DesktopKnowledgeBaseNotFoundError):
        DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert not (state_dir / "initializing").exists()
    assert not (state_dir / "state.sqlite3").exists()
    assert DesktopKnowledgeBaseRuntime().create(kb_dir).knowledge_base.name == "interrupted"
