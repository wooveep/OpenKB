"""Clean-cutover checks for the domain-neutral semantic authority schema."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from openkb.desktop_workspace import (
    DESKTOP_SCHEMA_EPOCH,
    DesktopKnowledgeBaseRuntime,
    ObsoleteDesktopKnowledgeBaseEpochError,
    desktop_state_database_path,
)


def test_current_semantic_epoch_reopens_idempotently(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    runtime = DesktopKnowledgeBaseRuntime()
    created = runtime.create(kb_dir).knowledge_base

    first = DesktopKnowledgeBaseRuntime().open(kb_dir).knowledge_base
    second = DesktopKnowledgeBaseRuntime().open(kb_dir).knowledge_base

    assert first.schema_version == created.schema_version == second.schema_version
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_epoch'"
        ).fetchone() == (DESKTOP_SCHEMA_EPOCH,)
        versions = [
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        assert versions == list(range(1, created.schema_version + 1))
        candidate_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(knowledge_candidate_generation_candidates)"
            )
        }
        assert {"identity_labels_json", "admission_state"} <= candidate_columns
        assert {"entity_subtype", "tags_json"}.isdisjoint(candidate_columns)
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "model_capability_compatibility_audit" not in tables


def test_obsolete_epoch_is_rejected_before_any_database_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb_dir = tmp_path / "obsolete"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM metadata WHERE key = 'schema_epoch'")
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()
    migration_called = False

    def unexpected_migration(*_args: object, **_kwargs: object) -> int:
        nonlocal migration_called
        migration_called = True
        raise AssertionError("obsolete databases must not enter migration")

    monkeypatch.setattr(
        "openkb.desktop_workspace.migrate_existing_database",
        unexpected_migration,
    )

    with pytest.raises(ObsoleteDesktopKnowledgeBaseEpochError) as captured:
        DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert captured.value.code == "desktop_knowledge_base_epoch_obsolete"
    assert "Create a new Knowledge Base" in str(captured.value)
    assert not migration_called
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before
