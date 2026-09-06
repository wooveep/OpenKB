"""Truthful import steps and sanitized model-wait state."""

from __future__ import annotations

from openkb.importing.service import DesktopTextImportService
from openkb.models.gateway import DesktopModelRequest
from openkb.models.terminal import DesktopTerminalModelEvent
from openkb.models.usage import DesktopModelUsageStore
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


def test_import_task_exposes_distinct_durable_pipeline_steps(tmp_path):
    kb_dir = tmp_path / "kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir, name="Progress KB")
    source = tmp_path / "release-notes.txt"
    source.write_text("# Release\n\nThe release is available.", encoding="utf-8")

    result = DesktopTextImportService(kb_dir).import_text(source)
    task = DesktopTextImportService(kb_dir).task(result.job.job_id).as_dict()

    assert [step["stage"] for step in task["import_progress"]] == [
        "preflight",
        "raw_asset",
        "parser_initialization",
        "document_ir",
        "evidence",
        "knowledge_analysis_plan",
        "knowledge_analysis_batches",
        "knowledge_analysis_merge",
        "publication",
    ]
    parser = task["import_progress"][2]
    assert parser["runtime_kind"] == "parser"
    assert parser["parser_family"] == "text"
    assert parser["parser_route"] == "plain_text"
    assert parser["parser_resource_state"] == "resources_ready"
    assert parser["parser_runtime_state"] == "ready"
    assert [task["import_progress"][index]["runtime_kind"] for index in (5, 6, 7)] == [
        "model",
        "model",
        "model",
    ]
    assert task["import_progress"][6]["completed"] == 0
    assert task["import_progress"][6]["total"] == 0
    assert all("percent" not in step for step in task["import_progress"])


def test_model_wait_projection_has_truthful_state_advisory_and_cancellation(tmp_path):
    kb_dir = tmp_path / "kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir, name="Wait KB")
    source = tmp_path / "release-notes.txt"
    source.write_text("Release notes", encoding="utf-8")
    result = DesktopTextImportService(kb_dir).import_text(source)
    task = DesktopTextImportService(kb_dir).task(result.job.job_id)
    analysis_stage = next(stage for stage in task.stages if stage.stage == "model_analysis")
    request = DesktopModelRequest(
        "knowledge_analysis_batch",
        "private-name.docx",
        "private source",
        model_role="analysis",
        model_name="analysis-model",
        job_id=result.job.job_id,
        stage_run_id=analysis_stage.stage_run_id,
        batch_id="batch-2",
    )
    usage = DesktopModelUsageStore(kb_dir)
    for status, elapsed in (
        ("queued", 0.0),
        ("connecting", 1.0),
        ("awaiting_model_result", 301.0),
    ):
        usage.record_event(
            request=request,
            event=DesktopTerminalModelEvent("call-wait", 1, status, elapsed),
            provider="custom",
            model="analysis-model",
        )

    payload = DesktopTextImportService(kb_dir).task(result.job.job_id).as_dict()
    activity = payload["model_activity"]
    assert activity["status"] == "awaiting_first_result"
    assert activity["operation"] == "knowledge_analysis_batch"
    assert activity["model_role"] == "analysis"
    assert activity["call_id"] == "call-wait"
    assert activity["attempt_id"] == "call-wait:1"
    assert activity["batch_id"] == "batch-2"
    assert activity["elapsed_seconds"] >= 301
    assert activity["long_wait_advisory"] is True
    assert activity["long_wait_threshold_seconds"] == 300
    assert activity["available_actions"] == ["cancel"]
    assert "percent" not in activity
    assert "private" not in repr(payload["model_usage"])
