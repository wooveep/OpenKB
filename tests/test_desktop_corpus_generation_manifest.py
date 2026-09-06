"""Atomic activation behavior for generation-scoped corpus manifests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openkb.desktop_candidate_registry import publish_candidate_registry_generation_in
from openkb.desktop_corpus_synthesis_generation import (
    activate_qualified_corpus_generation_in,
    corpus_generation_manifest_in,
    create_pending_corpus_manifest_in,
    qualify_corpus_manifest_in,
)
from openkb.desktop_corpus_synthesis_tasks import claim_corpus_synthesis_task_in
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def _import_document_without_background_graph(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nA stable source block.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    return kb_dir, document.document_id


def _publish_empty_candidate_generation(
    connection: sqlite3.Connection, document_id: str, checkpoint: str
) -> str:
    outcome = publish_candidate_registry_generation_in(
        connection,
        document_id=document_id,
        analysis_provenance_json=json.dumps({"checkpoint_digest": checkpoint}),
        now=f"2026-09-04T00:00:0{checkpoint[-1]}+00:00",
    )
    assert outcome.generation is not None
    return outcome.generation.generation_id


def _pending_generation(connection: sqlite3.Connection, document_id: str, ordinal: int) -> int:
    now = f"2026-09-04T00:01:0{ordinal}+00:00"
    cursor = connection.execute(
        "INSERT INTO knowledge_generations (created_at, qualification_state) "
        "VALUES (?, 'candidate')",
        (now,),
    )
    assert cursor.lastrowid is not None
    generation_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO knowledge_generation_documents (generation_id, document_id) VALUES (?, ?)",
        (generation_id, document_id),
    )
    create_pending_corpus_manifest_in(
        connection,
        generation_id=generation_id,
        parent_generation_id=None,
        document_ids=(document_id,),
        now=now,
    )
    assert qualify_corpus_manifest_in(connection, generation_id, now=now) == ()
    return generation_id


def test_superseded_candidate_input_cannot_move_corpus_current_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    kb_dir, document_id = _import_document_without_background_graph(tmp_path, monkeypatch)
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        first_candidate_id = _publish_empty_candidate_generation(
            connection, document_id, "candidate-1"
        )
        first_generation_id = _pending_generation(connection, document_id, 1)
        assert activate_qualified_corpus_generation_in(
            connection, first_generation_id, now="2026-09-04T00:02:01+00:00"
        )

        second_candidate_id = _publish_empty_candidate_generation(
            connection, document_id, "candidate-2"
        )
        second_generation_id = _pending_generation(connection, document_id, 2)
        third_candidate_id = _publish_empty_candidate_generation(
            connection, document_id, "candidate-3"
        )

        assert not activate_qualified_corpus_generation_in(
            connection, second_generation_id, now="2026-09-04T00:02:02+00:00"
        )
        assert connection.execute(
            "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
        ).fetchone() == (first_generation_id,)
        manifest = corpus_generation_manifest_in(connection, second_generation_id)

    assert len({first_candidate_id, second_candidate_id, third_candidate_id}) == 3
    assert manifest is not None
    assert manifest.lifecycle_state == "superseded"


def test_open_recovers_an_interrupted_pending_corpus_generation_without_model_work(
    tmp_path: Path, monkeypatch
) -> None:
    kb_dir, document_id = _import_document_without_background_graph(tmp_path, monkeypatch)
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        _publish_empty_candidate_generation(connection, document_id, "candidate-1")
        now = "2026-09-04T00:01:01+00:00"
        cursor = connection.execute(
            "INSERT INTO knowledge_generations (created_at, qualification_state) "
            "VALUES (?, 'candidate')",
            (now,),
        )
        assert cursor.lastrowid is not None
        generation_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO knowledge_generation_documents (generation_id, document_id) VALUES (?, ?)",
            (generation_id, document_id),
        )
        create_pending_corpus_manifest_in(
            connection,
            generation_id=generation_id,
            parent_generation_id=None,
            document_ids=(document_id,),
            now=now,
        )
        claim_corpus_synthesis_task_in(
            connection,
            generation_id,
            provider="scripted",
            model="page-planner-v1",
            retry_scope=None,
            now=now,
        )

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            "SELECT lifecycle_state, page_state "
            "FROM knowledge_generation_manifests WHERE generation_id = ?",
            (generation_id,),
        ).fetchone() == ("failed", "failed")
        assert connection.execute(
            "SELECT qualification_state FROM knowledge_generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT status, phase, error_code, execution_token "
            "FROM knowledge_corpus_synthesis_tasks WHERE generation_id = ?",
            (generation_id,),
        ).fetchone() == (
            "failed",
            "failed",
            "corpus_synthesis_interrupted",
            None,
        )
