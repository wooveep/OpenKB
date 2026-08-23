"""End-to-end terminal-event semantics for every Desktop model workload."""

from __future__ import annotations

import json
import threading

import pytest

from openkb.desktop_grounded_answer import DesktopGroundedAnswerService
from openkb.desktop_import import DesktopImportControl, DesktopImportError, DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_model_gateway import DesktopModelRequest
from openkb.desktop_model_terminal import DesktopTerminalModelGateway
from openkb.desktop_model_transport import (
    _ConcurrentDesktopModelTransport,
    _DesktopModelConcurrencyGate,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


class _Clock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


def _analysis() -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "A usable document.",
            "concepts": [],
            "entities": [],
        }
    )


class _SilentTerminalProvider:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.operations: list[str] = []

    def __call__(self, request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
        return self.call_until_terminal_with_lifecycle(
            request, _connect_timeout_seconds, lambda: None
        )

    def call_until_terminal_with_lifecycle(
        self,
        request: DesktopModelRequest,
        _connect_timeout_seconds: float,
        on_request_sent,
    ) -> str:
        self.operations.append(request.operation)
        on_request_sent()
        self.clock.value += 180
        if request.operation == "knowledge_analysis":
            return _analysis()
        if request.operation == "retrieval_plan":
            return json.dumps({"terms": ["OpenKB", "evidence"]})
        if request.operation == "grounded_answer":
            return "OpenKB keeps cited evidence available."
        raise AssertionError(request.operation)


def test_required_knowledge_analysis_succeeds_after_180_seconds_of_model_thinking(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.txt"
    source.write_text("OpenKB keeps source-grounded knowledge available.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    clock = _Clock()
    provider = _SilentTerminalProvider(clock)
    gateway = DesktopTerminalModelGateway(
        provider,
        clock=clock,
        provider_name="scripted",
        model_name="reasoning-model",
    )

    imported = DesktopTextImportService(
        kb_dir,
        model_gateway=gateway,
        require_model_analysis=True,
    ).import_text(source)

    assert imported.document.availability == "available"
    assert provider.operations == ["knowledge_analysis"]
    assert imported.model_calls[-1].status == "completed"
    assert imported.model_calls[-1].attempts[-1].elapsed_seconds == 180
    assert all(call.error_code != "model_deadline_exceeded" for call in imported.model_calls)


def test_grounded_answer_succeeds_after_terminal_model_calls_each_wait_180_seconds(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.txt"
    source.write_text("OpenKB keeps cited evidence available for answers.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    clock = _Clock()
    provider = _SilentTerminalProvider(clock)
    gateway = DesktopTerminalModelGateway(
        provider,
        clock=clock,
        provider_name="scripted",
        model_name="answer-model",
    )

    answer = DesktopGroundedAnswerService(kb_dir, model_gateway=gateway).answer(
        "What does OpenKB keep?"
    )

    assert answer.status == "completed"
    assert answer.answer_text == "OpenKB keeps cited evidence available."
    assert provider.operations[:2] == ["retrieval_plan", "grounded_answer"]
    assert clock.value >= 360


def test_cancelling_a_silent_analysis_preserves_manual_resume_checkpoints(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.txt"
    source.write_text(
        "OpenKB preserves completed parser and evidence checkpoints.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    control = DesktopImportControl()
    started = threading.Event()
    release = threading.Event()
    failures: list[Exception] = []

    class BlockingProvider:
        def __call__(self, _request, _connect_timeout_seconds):
            started.set()
            release.wait(timeout=2)
            return _analysis()

    importer = DesktopTextImportService(
        kb_dir,
        control=control,
        model_gateway=DesktopTerminalModelGateway(BlockingProvider()),
        require_model_analysis=True,
    )

    def run() -> None:
        try:
            importer.import_text(source)
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=1)
    control.request_cancel()
    worker.join(timeout=2)
    release.set()

    assert len(failures) == 1
    assert isinstance(failures[0], DesktopImportError)
    assert failures[0].code == "import_interrupted"
    task = importer.list_import_jobs()["jobs"][0]
    assert task["job"]["status"] == "paused"
    assert [stage["status"] for stage in task["stages"][:5]] == ["completed"] * 5
    assert task["document"] is None

    recovered = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopTerminalModelGateway(lambda *_args: _analysis()),
        require_model_analysis=True,
    ).resume_text(task["job"]["job_id"])
    assert recovered.document.availability == "available"


def test_interactive_lane_is_not_starved_by_indefinite_background_analysis() -> None:
    background_started = threading.Event()
    release_background = threading.Event()
    interactive_finished = threading.Event()

    def provider(request, _connect_timeout_seconds):
        if request.operation == "knowledge_analysis":
            background_started.set()
            release_background.wait(timeout=2)
            return _analysis()
        return "interactive answer"

    background_gate = _DesktopModelConcurrencyGate(1)
    interactive_gate = _DesktopModelConcurrencyGate(1)
    transport = _ConcurrentDesktopModelTransport(
        provider,
        background_gate,
        lane_factory=lambda lane: interactive_gate if lane == "interactive" else background_gate,
    )
    background = DesktopTerminalModelGateway(transport)
    interactive = background.for_lane("interactive")
    worker = threading.Thread(
        target=lambda: background.analyze(
            DesktopModelRequest("knowledge_analysis", "document", "content"),
            on_event=lambda _event: None,
        )
    )
    worker.start()
    assert background_started.wait(timeout=1)

    def ask() -> None:
        interactive.analyze(
            DesktopModelRequest("grounded_answer", "question", "content"),
            on_event=lambda _event: None,
        )
        interactive_finished.set()

    answer_worker = threading.Thread(target=ask)
    answer_worker.start()
    assert interactive_finished.wait(timeout=0.5)
    release_background.set()
    worker.join(timeout=1)
    answer_worker.join(timeout=1)


def test_usable_document_waits_for_model_configuration_and_resumes_explicitly(
    kb_dir, tmp_path
) -> None:
    kb_dir = kb_dir / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    source = tmp_path / "awaiting-model.txt"
    source.write_text("Evidence remains reusable while model settings are missing.")
    importer = DesktopTextImportService(kb_dir, require_model_analysis=True)

    with pytest.raises(DesktopImportError) as captured:
        importer.import_text(source)

    assert captured.value.code == "awaiting_model_configuration"
    task = importer.list_import_jobs()["jobs"][0]
    assert task["job"]["status"] == "awaiting_model_configuration"
    assert task["document"] is None
    model_stage = next(stage for stage in task["stages"] if stage["stage"] == "model_analysis")
    assert model_stage["status"] == "paused"
    assert model_stage["error_code"] == "awaiting_model_configuration"

    resumed = DesktopTextImportService(
        kb_dir,
        require_model_analysis=True,
        model_gateway=DesktopTerminalModelGateway(lambda *_args: _analysis()),
    ).resume_text(task["job"]["job_id"])
    assert resumed.document.availability == "available"


def test_background_model_gate_dispatches_waiting_documents_in_fifo_order() -> None:
    release_first = threading.Event()
    first_started = threading.Event()
    queued_second = threading.Event()
    queued_third = threading.Event()
    provider_order: list[str] = []

    def provider(request, _connect_timeout_seconds):
        provider_order.append(request.document_name)
        if request.document_name == "first":
            first_started.set()
            release_first.wait(timeout=2)
        return "complete"

    gateway = DesktopTerminalModelGateway(
        _ConcurrentDesktopModelTransport(provider, _DesktopModelConcurrencyGate(1))
    )

    def run(name: str, queued: threading.Event | None = None) -> None:
        gateway.analyze(
            DesktopModelRequest("knowledge_analysis", name, "content"),
            on_event=lambda event: (
                queued.set() if queued is not None and event.status == "queued" else None
            ),
        )

    first = threading.Thread(target=run, args=("first",))
    second = threading.Thread(target=run, args=("second", queued_second))
    third = threading.Thread(target=run, args=("third", queued_third))
    first.start()
    assert first_started.wait(timeout=1)
    second.start()
    assert queued_second.wait(timeout=1)
    third.start()
    assert queued_third.wait(timeout=1)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)
    third.join(timeout=1)

    assert provider_order == ["first", "second", "third"]
