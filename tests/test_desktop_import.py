"""Focused behavior checks for the first Desktop-native TXT import path."""

from __future__ import annotations

import json
import sqlite3
import threading
from hashlib import sha256

import pytest

from openkb.documents.versions import DesktopDocumentVersionService
from openkb.importing import runner as desktop_import_runner
from openkb.importing.service import (
    DesktopImportControl,
    DesktopImportError,
    DesktopRecoveryOverride,
    DesktopTextImportService,
)
from openkb.importing.store import DesktopImportStore
from openkb.knowledge.analysis.service import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.models.gateway import (
    DesktopModelGateway,
    DesktopModelOutputObservations,
    DesktopModelProviderResponse,
    DesktopProviderTokenUsage,
)
from openkb.retrieval.service import DesktopEvidenceRetriever
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


def _empty_analysis() -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "No durable knowledge candidates.",
            "document_summary": [],
            "candidates": [],
        }
    )


def test_txt_import_publishes_raw_ir_evidence_and_fts_in_one_available_document(tmp_path):
    """A successful TXT import produces every retrieval baseline artifact exactly once."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_text(
        "# Getting started\n\nOpenKB keeps local knowledge searchable.\n", encoding="utf-8"
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    events: list[dict[str, object]] = []

    result = DesktopTextImportService(kb_dir, on_stage_progress=events.append).import_text(source)

    assert result.document.name == "guide.txt"
    assert result.document.availability == "available"
    assert result.document.evidence_count == 2
    assert result.job.status == "completed"
    assert [stage.status for stage in result.stages] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "completed",
        "skipped",
        "completed",
    ]
    assert [(event["stage"], event["status"]) for event in events] == [
        ("preflight", "running"),
        ("preflight", "completed"),
        ("raw_asset", "running"),
        ("raw_asset", "completed"),
        ("document_ir", "running"),
        ("document_ir", "completed"),
        ("evidence", "running"),
        ("evidence", "completed"),
        ("deterministic_page_tree", "running"),
        ("deterministic_page_tree", "completed"),
        ("model_analysis", "running"),
        ("model_analysis", "skipped"),
        ("search", "running"),
        ("search", "completed"),
    ]

    raw_files = list((kb_dir / "raw").iterdir())
    assert [path.name for path in raw_files] == [f"{result.document.raw_asset_sha256}.txt"]
    assert raw_files[0].read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_assets").fetchone() == (1,)
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (result.document.document_id,),
        ).fetchone() == ("available",)
        assert connection.execute("SELECT COUNT(*) FROM document_ir_blocks").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_refs").fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_fts WHERE evidence_fts MATCH 'searchable'"
        ).fetchone() == (1,)


def test_duplicate_txt_reuses_the_single_available_raw_asset(tmp_path):
    """D0-identical input is immediately available without another source document."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "same.txt"
    source.write_text("Same content.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)

    first = importer.import_text(source)
    second = importer.import_text(source)

    assert second.job.deduplicated is True
    assert second.document.document_id == first.document.document_id
    assert [stage.status for stage in second.stages] == [
        "completed",
        "completed",
        "skipped",
        "skipped",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert len(list((kb_dir / "raw").iterdir())) == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (1,)

    history = importer.list_import_jobs()["jobs"]
    assert history[0]["job"]["deduplicated"] is True


def test_d1_normalized_body_reuses_processing_but_keeps_a_distinct_raw_document(tmp_path):
    """Equivalent normalized bodies retain two versions while reusing later checkpoints."""
    kb_dir = tmp_path / "desktop-kb"
    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_source.write_text("# Guide\n\nSame normalized body.\n", encoding="utf-8")
    second_source.write_bytes(b"# Guide\r\n\r\nSame normalized body.  \r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)

    first = importer.import_text(first_source)
    second = importer.import_text(second_source)

    assert second.document.document_id != first.document.document_id
    assert second.document.raw_asset_sha256 != first.document.raw_asset_sha256
    assert second.job.deduplication is not None
    assert second.job.deduplication.level == "D1"
    assert second.job.deduplication.reused_document_id == first.document.document_id
    assert second.job.deduplication.reusable_stages == ("evidence", "model_analysis", "search")
    assert [stage.status for stage in second.stages] == [
        "completed",
        "completed",
        "completed",
        "skipped",
        "completed",
        "skipped",
        "completed",
    ]
    assert len(list((kb_dir / "raw").iterdir())) == 2

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM document_ir_blocks").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_refs").fetchone() == (2,)
        assert connection.execute(
            """
            SELECT canonical_document_id FROM document_content_fingerprints
            WHERE document_id = ?
            """,
            (second.document.document_id,),
        ).fetchone() == (first.document.document_id,)

    history = importer.list_import_jobs()["jobs"]
    assert history[0]["job"]["deduplication"]["reason"] == "normalized_body_sha256_match"


def test_d2_reuses_duplicate_evidence_without_merging_document_identity(tmp_path):
    """A repeated fragment adds an occurrence, not a second independent EvidenceRef."""
    kb_dir = tmp_path / "desktop-kb"
    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_source.write_text("# Guide\n\nShared evidence.\n\nFirst-only evidence.", encoding="utf-8")
    second_source.write_text(
        "# Guide\n\nShared evidence.\n\nSecond-only evidence.", encoding="utf-8"
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)

    first = importer.import_text(first_source)
    second = importer.import_text(second_source)

    assert second.document.document_id != first.document.document_id
    assert second.job.deduplication is not None
    assert second.job.deduplication.level == "D2"
    assert second.job.deduplication.reason == "evidence_sha256_match"
    assert second.job.deduplication.reused_evidence_count == 2
    assert second.job.deduplication.reusable_stages == ("evidence",)
    pack = DesktopEvidenceRetriever(kb_dir).retrieve("Shared evidence")
    assert [reference.excerpt for reference in pack.evidence].count("Shared evidence.") == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_refs").fetchone() == (4,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_occurrences").fetchone() == (6,)
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_refs WHERE text = 'Shared evidence.'"
        ).fetchone() == (1,)
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (first.document.document_id,),
        )

    fallback_pack = DesktopEvidenceRetriever(kb_dir).retrieve("Shared evidence")
    shared_reference = next(
        reference for reference in fallback_pack.evidence if reference.excerpt == "Shared evidence."
    )
    assert shared_reference.document_id == second.document.document_id
    assert shared_reference.document_name == second.document.name


def test_d3_candidate_never_links_versions_until_the_user_confirms(tmp_path):
    """A bounded lexical/character suggestion is review-only until it is accepted."""
    kb_dir = tmp_path / "desktop-kb"
    first_source = tmp_path / "architecture-notes.txt"
    second_source = tmp_path / "architecture-notes-v2.txt"
    first_source.write_text(
        "# Architecture notes\n\nOpenKB Desktop stores imported files in raw assets and uses "
        "SQLite evidence blocks for local retrieval. A user reviews related document versions "
        "before any source history is linked.",
        encoding="utf-8",
    )
    second_source.write_text(
        "# Architecture notes v2\n\nOpenKB Desktop stores imported files in raw assets and uses "
        "SQLite evidence blocks for local retrieval. A user reviews related document versions "
        "before any source history is linked. This version improves the review display.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)

    first = importer.import_text(first_source)
    second = importer.import_text(second_source)
    versions = DesktopDocumentVersionService(kb_dir)
    (candidate,) = versions.list_candidates()

    assert candidate.document_id == second.document.document_id
    assert candidate.candidate_document_id == first.document.document_id
    assert candidate.reason == "lexical_character_similarity"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT source_id FROM document_version_members WHERE document_id = ?",
            (second.document.document_id,),
        ).fetchone() == (second.document.document_id,)

    resolved = versions.resolve_candidate(candidate.candidate_id, "link_to_candidate")

    assert resolved.status == "accepted"
    assert versions.list_candidates() == ()
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT source_id FROM document_version_members WHERE document_id = ?",
            (second.document.document_id,),
        ).fetchone() == (first.document.document_id,)


def test_d3_rejection_keeps_a_document_as_an_independent_source(tmp_path):
    """Rejecting a D3 suggestion has no hidden automatic source merge."""
    kb_dir = tmp_path / "desktop-kb"
    first_source = tmp_path / "roadmap.txt"
    second_source = tmp_path / "roadmap-draft.txt"
    first_source.write_text(
        "# Product roadmap\n\nThe desktop workbench imports local documents, builds evidence, "
        "and keeps source records available for grounded answers.",
        encoding="utf-8",
    )
    second_source.write_text(
        "# Product roadmap draft\n\nThe desktop workbench imports local documents, "
        "builds evidence, "
        "and keeps source records available for grounded answers. This is a separate proposal.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)

    importer.import_text(first_source)
    second = importer.import_text(second_source)
    versions = DesktopDocumentVersionService(kb_dir)
    (candidate,) = versions.list_candidates()

    resolved = versions.resolve_candidate(candidate.candidate_id, "keep_separate")

    assert resolved.status == "rejected"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT source_id FROM document_version_members WHERE document_id = ?",
            (second.document.document_id,),
        ).fetchone() == (second.document.document_id,)


def test_failed_prepublication_stage_never_exposes_a_partial_document(
    tmp_path, monkeypatch, caplog
):
    """A crash-like stage failure can leave raw recovery input but not Available Knowledge."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "broken.txt"
    source.write_text("Known source text.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def fail_document_ir(*_args, **_kwargs):
        raise DesktopImportError("simulated_document_ir_failure", "Simulated Document IR failure.")

    monkeypatch.setattr(desktop_import_runner, "build_document_ir", fail_document_ir)
    with pytest.raises(DesktopImportError, match="Simulated Document IR failure"):
        DesktopTextImportService(kb_dir).import_text(source)

    assert len(list((kb_dir / "raw").iterdir())) == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_fts").fetchone() == (0,)
        assert connection.execute("SELECT status FROM import_jobs").fetchone() == ("failed",)
    terminal_log = next(record for record in caplog.records if record.msg == "import_failed")
    fields = terminal_log.openkb_fields
    assert fields["stage"] == "document_ir"
    assert fields["last_completed_stage"] == "raw_asset"
    assert fields["error_code"] == "simulated_document_ir_failure"
    assert fields["next_action"] == "inspect_source_or_convert_format"
    assert fields["source_extension"] == ".txt"
    assert "broken.txt" not in str(fields)
    assert fields["failure_event_id"]


def test_model_failure_is_quarantined_with_safe_attempt_history(tmp_path):
    """A terminal Model Call leaves retriable evidence visible, never published."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "slow.txt"
    source.write_text("Model analysis must not publish after repeated timeouts.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def timeout(*_args, **_kwargs):
        raise TimeoutError("provider detail must never be persisted: api_key=secret")

    importer = DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(timeout))
    with pytest.raises(DesktopImportError) as error:
        importer.import_text(source)

    assert error.value.code == "document_quarantined"
    task = importer.list_import_jobs()["jobs"][0]
    assert task["job"]["status"] == "quarantined"
    assert task["document"] is None
    assert task["quarantine"] == {
        "stage_run_id": task["stages"][5]["stage_run_id"],
        "stage": "model_analysis",
        "error_code": "model_network_transient",
        "reason": "The connection to the model provider failed or was interrupted.",
        "suggested_action": "Check the network connection, then retry.",
        "attempt_count": 3,
    }
    assert len(task["model_calls"]) == 1
    model_call = task["model_calls"][0]
    assert model_call["status"] == "failed"
    assert model_call["lifecycle_status"] == "network_failure"
    assert model_call["attempt_count"] == 3
    assert {attempt["status"] for attempt in model_call["attempts"]}.issubset(
        {"running", "retry_wait", "completed", "failed"}
    )
    assert model_call["attempts"][-1]["status"] == "failed"
    assert model_call["attempts"][-1]["lifecycle_status"] == "network_failure"
    assert all("timeout_seconds" not in attempt for attempt in model_call["attempts"])
    assert "api_key" not in str(task)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_fts").fetchone() == (0,)


def test_reasoning_exhaustion_is_a_single_safe_model_result_failure(tmp_path, caplog):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "analysis.txt"
    source.write_text("Structured Analysis must produce final JSON.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def reasoning_only(*_args, **_kwargs):
        return DesktopModelProviderResponse(
            "",
            usage=DesktopProviderTokenUsage(20, 80, 100),
            observations=DesktopModelOutputObservations(
                finish_reason="length",
                reasoning_observed=True,
                final_content_observed=False,
                reasoning_chunk_count=4,
                final_chunk_count=0,
                reasoning_character_count=512,
                final_character_count=0,
                output_limit_reached=True,
            ),
        )

    importer = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(reasoning_only),
    )
    with pytest.raises(DesktopImportError) as captured:
        importer.import_text(source)

    assert captured.value.code == "document_quarantined"
    task = importer.list_import_jobs()["jobs"][0]
    call = task["model_calls"][0]
    assert call["status"] == "failed"
    assert call["lifecycle_status"] == "model_result_failure"
    assert call["error_code"] == "reasoning_output_exhausted"
    assert call["attempt_count"] == 1
    assert call["finish_reason"] == "length"
    assert call["reasoning_observed"] is True
    assert call["final_content_observed"] is False
    assert call["reasoning_chunk_count"] == 4
    assert call["reasoning_character_count"] == 512
    assert call["input_tokens"] == 20
    assert call["output_tokens"] == 80
    assert call["total_tokens"] == 100
    assert "Structured Analysis" not in json.dumps(task)
    terminal_log = next(record for record in caplog.records if record.msg == "model_call_failed")
    fields = terminal_log.openkb_fields
    assert fields["error_code"] == "reasoning_output_exhausted"
    assert fields["failure_kind"] == "model_result_failure"
    assert fields["finish_reason"] == "length"
    assert fields["reasoning_observed"] is True
    assert fields["final_content_observed"] is False
    assert fields["reasoning_chunk_count"] == 4
    assert fields["final_chunk_count"] == 0
    assert fields["reasoning_character_count"] == 512
    assert fields["final_character_count"] == 0
    assert fields["input_tokens"] == 20
    assert fields["output_tokens"] == 80
    assert fields["total_tokens"] == 100
    assert fields["outcome"] == "failed"
    assert fields["next_action"] == "run_model_capability_check"


def test_manual_recovery_reuses_verified_stages_and_records_its_override(tmp_path, monkeypatch):
    """A quarantined model stage resumes without rebuilding raw, IR, or evidence work."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "recover.txt"
    source.write_text("# Recovery\n\nKeep verified artifacts.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def timeout(*_args, **_kwargs):
        raise TimeoutError()

    with pytest.raises(DesktopImportError, match="connection"):
        DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(timeout)).import_text(
            source
        )

    DesktopKnowledgeBaseRuntime().open(kb_dir)
    persisted = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"][0]
    job_id = persisted["job"]["job_id"]
    assert persisted["job"]["status"] == "quarantined"
    assert persisted["job"]["source_name"] == "recover.txt"
    assert persisted["quarantine"]["stage"] == "model_analysis"
    assert len(persisted["model_calls"][0]["attempts"]) == 3

    source.unlink()
    monkeypatch.setattr(
        desktop_import_runner,
        "build_document_ir",
        lambda *_args, **_kwargs: pytest.fail("recovery rebuilt Document IR"),
    )
    monkeypatch.setattr(
        desktop_import_runner,
        "build_evidence",
        lambda *_args, **_kwargs: pytest.fail("recovery rebuilt evidence"),
    )
    recovered = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(lambda *_args: _empty_analysis()),
    ).recover_text(
        job_id,
        DesktopRecoveryOverride(model="test/recovery-model"),
    )

    assert recovered.job.status == "completed"
    assert recovered.quarantine is None
    assert recovered.document.availability == "available"
    assert [call.status for call in recovered.model_calls] == [
        "failed",
        "completed",
    ]
    assert [call.lifecycle_status for call in recovered.model_calls] == [
        "network_failure",
        "completed",
    ]
    assert recovered.model_calls[-1].attempts[0].status == "completed"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM quarantined_documents").fetchone() == (0,)
        assert connection.execute(
            "SELECT model_override, initial_timeout_seconds, status FROM recovery_runs"
        ).fetchone() == ("test/recovery-model", None, "completed")
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (1,)


def test_cancelled_manual_recovery_can_be_retried(tmp_path):
    """Cancelling a recovery keeps its quarantined document manually recoverable."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "retry-after-cancel.txt"
    source.write_text("Keep checkpoints after a cancelled recovery.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def timeout(*_args, **_kwargs):
        raise TimeoutError()

    with pytest.raises(DesktopImportError):
        DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(timeout)).import_text(
            source
        )
    job_id = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"][0]["job"]["job_id"]

    control = DesktopImportControl()
    control.request_cancel()
    with pytest.raises(DesktopImportError) as error:
        DesktopTextImportService(
            kb_dir,
            control=control,
            model_gateway=DesktopModelGateway(lambda *_args: "not called"),
        ).recover_text(job_id, DesktopRecoveryOverride())

    assert error.value.code == "import_cancelled"
    cancelled = DesktopTextImportService(kb_dir).task(job_id)
    assert cancelled.job.status == "quarantined"
    assert cancelled.quarantine is not None

    recovered = DesktopTextImportService(
        kb_dir, model_gateway=DesktopModelGateway(lambda *_args: _empty_analysis())
    ).recover_text(job_id, DesktopRecoveryOverride())

    assert recovered.job.status == "completed"
    assert recovered.quarantine is None


def test_recovery_with_an_invalid_raw_checkpoint_stays_quarantined(tmp_path):
    """Checkpoint validation blocks recovery before an invalid document reaches retrieval."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "damaged.txt"
    source.write_text("Do not publish an invalid recovery.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def timeout(*_args, **_kwargs):
        raise TimeoutError()

    with pytest.raises(DesktopImportError):
        DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(timeout)).import_text(
            source
        )
    job_id = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"][0]["job"]["job_id"]
    next((kb_dir / "raw").iterdir()).write_text("tampered", encoding="utf-8")

    with pytest.raises(DesktopImportError) as error:
        DesktopTextImportService(
            kb_dir, model_gateway=DesktopModelGateway(lambda *_args: "never used")
        ).recover_text(job_id, DesktopRecoveryOverride())

    assert error.value.code == "raw_asset_integrity_failed"
    task = DesktopTextImportService(kb_dir).task(job_id)
    assert task.job.status == "quarantined"
    assert task.quarantine is not None
    assert task.quarantine.stage == "raw_asset"
    assert task.quarantine.error_code == "raw_asset_integrity_failed"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)


def test_open_recovers_an_interrupted_job_without_exposing_a_partial_document(tmp_path):
    """Restarting exposes a pre-publication crash as durable recoverable work."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "interrupted.txt"
    source.write_text("Recoverable raw source.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopImportStore(kb_dir)
    state = store.create_job(source)
    store.set_stage(
        state,
        "preflight",
        "completed",
        20,
        checkpoint={"asset_sha256": "a" * 64, "raw_size": len(source.read_bytes())},
    )
    raw_path = store.write_raw_asset("a" * 64, source.read_bytes())
    store.set_stage(
        state,
        "raw_asset",
        "completed",
        35,
        checkpoint={
            "asset_sha256": "a" * 64,
            "raw_path": raw_path,
            "raw_size": len(source.read_bytes()),
        },
    )

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    history = store.list_import_jobs()["jobs"]
    assert history[0]["job"]["status"] == "recoverable"
    assert history[0]["document"] is None
    assert [stage["status"] for stage in history[0]["stages"]] == [
        "completed",
        "completed",
        "pending",
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)


def test_resume_revalidates_preflight_when_the_source_changed_before_raw_asset(tmp_path):
    """A stale preflight checkpoint cannot certify a later version of a source file."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "changed.txt"
    original = b"Original source."
    replacement = b"Replacement source."
    source.write_bytes(original)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopImportStore(kb_dir)
    state = store.create_job(source)
    store.set_stage(
        state,
        "preflight",
        "completed",
        20,
        checkpoint={"asset_sha256": sha256(original).hexdigest(), "raw_size": len(original)},
    )
    store.pause_job(state, "raw_asset")
    source.write_bytes(replacement)

    result = DesktopTextImportService(kb_dir).resume_text(state.job_id)

    assert result.document.raw_asset_sha256 == sha256(replacement).hexdigest()
    assert (kb_dir / "raw" / f"{sha256(replacement).hexdigest()}.txt").read_bytes() == replacement


def test_open_marks_an_expired_running_lease_as_recoverable(tmp_path):
    """A restart labels an expired worker lease distinctly from an abrupt handoff."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "expired.txt"
    source.write_text("Lease recovery.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopImportStore(kb_dir)
    state = store.create_job(source)
    store.set_stage(state, "preflight", "running", 0)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE import_job_runtime SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
        )

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    task = DesktopTextImportService(kb_dir).task(state.job_id)
    assert task.job.status == "recoverable"
    assert task.stages[0].status == "paused"
    assert task.stages[0].error_code == "import_lease_expired"


def test_open_waits_for_a_live_import_instead_of_recovering_it(tmp_path, monkeypatch):
    """A second Engine cannot misclassify a lock-owning import as a crash."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "live.txt"
    source.write_text("# Live import\n\nStill working.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document_ir_started = threading.Event()
    release_import = threading.Event()
    open_finished = threading.Event()
    outcomes: list[object] = []
    original_build_document_ir = desktop_import_runner.build_document_ir

    def wait_before_building_document_ir(*args, **kwargs):
        document_ir_started.set()
        assert release_import.wait(timeout=2)
        return original_build_document_ir(*args, **kwargs)

    monkeypatch.setattr(
        desktop_import_runner, "build_document_ir", wait_before_building_document_ir
    )

    def import_in_background() -> None:
        try:
            outcomes.append(DesktopTextImportService(kb_dir).import_text(source))
        except BaseException as error:  # Captured to make the thread assertion deterministic.
            outcomes.append(error)

    def open_in_background() -> None:
        try:
            outcomes.append(DesktopKnowledgeBaseRuntime().open(kb_dir))
        except BaseException as error:  # Captured to make the thread assertion deterministic.
            outcomes.append(error)
        finally:
            open_finished.set()

    importer_thread = threading.Thread(target=import_in_background)
    importer_thread.start()
    assert document_ir_started.wait(timeout=2)
    opener_thread = threading.Thread(target=open_in_background)
    opener_thread.start()
    assert not open_finished.wait(timeout=0.1)
    release_import.set()
    importer_thread.join(timeout=2)
    opener_thread.join(timeout=2)

    assert all(not isinstance(outcome, BaseException) for outcome in outcomes)
    history = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"]
    assert [task["job"]["status"] for task in history] == ["completed"]


def test_paused_import_resumes_from_verified_document_ir_checkpoint(tmp_path, monkeypatch):
    """A resumed job does not rerun the completed Document IR stage."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "resume.txt"
    source.write_text("# Resume\n\nKeep this completed stage.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    control = DesktopImportControl()
    calls = 0
    original_build_document_ir = desktop_import_runner.build_document_ir

    def pause_after_document_ir(*args, **kwargs):
        nonlocal calls
        calls += 1
        blocks = original_build_document_ir(*args, **kwargs)
        control.request_pause()
        return blocks

    monkeypatch.setattr(desktop_import_runner, "build_document_ir", pause_after_document_ir)
    with pytest.raises(DesktopImportError) as paused:
        DesktopTextImportService(kb_dir, control=control).import_text(source)
    assert paused.value.code == "import_paused"

    paused_task = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"][0]
    assert paused_task["job"]["status"] == "paused"
    assert [stage["status"] for stage in paused_task["stages"]] == [
        "completed",
        "completed",
        "completed",
        "paused",
        "pending",
        "pending",
        "pending",
    ]

    resumed = DesktopTextImportService(kb_dir).resume_text(paused_task["job"]["job_id"])

    assert resumed.job.status == "completed"
    assert calls == 1
    assert [stage.status for stage in resumed.stages] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "completed",
        "skipped",
        "completed",
    ]


def test_cancelled_paused_import_never_becomes_available_knowledge(tmp_path, monkeypatch):
    """A user can cancel after pause without publishing pre-publication artifacts."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "cancel.txt"
    source.write_text("# Cancel\n\nLeave this unavailable.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    control = DesktopImportControl()
    original_build_document_ir = desktop_import_runner.build_document_ir

    def pause_after_document_ir(*args, **kwargs):
        blocks = original_build_document_ir(*args, **kwargs)
        control.request_pause()
        return blocks

    monkeypatch.setattr(desktop_import_runner, "build_document_ir", pause_after_document_ir)
    with pytest.raises(DesktopImportError, match="paused"):
        DesktopTextImportService(kb_dir, control=control).import_text(source)

    job_id = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"][0]["job"]["job_id"]
    DesktopTextImportService(kb_dir).cancel_paused_job(job_id)

    cancelled = DesktopTextImportService(kb_dir).task(job_id)
    assert cancelled.job.status == "cancelled"
    assert cancelled.document is None
    assert cancelled.stages[3].status == "cancelled"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)
