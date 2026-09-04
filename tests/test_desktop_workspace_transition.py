"""Active Knowledge Base transition behavior through the Desktop Engine seam."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from openkb import desktop_engine_workspace_activation as workspace_activation_engine
from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
from openkb.desktop_import import DesktopImportControl, DesktopImportError
from openkb.desktop_import_store import DesktopImportStore
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_workspace import DesktopKnowledgeBaseError, DesktopKnowledgeBaseRuntime
from openkb.desktop_workspace_transition import DesktopWorkspaceTransitionCoordinator


def _empty_knowledge_analysis() -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "No durable knowledge candidates.",
            "concepts": [],
            "entities": [],
        }
    )


def _request(request_id: str, method: str, **params: object) -> DesktopRequest:
    return DesktopRequest(request_id=request_id, method=method, params=params)


def _observe_pause_requests(monkeypatch) -> threading.Event:
    pause_requested = threading.Event()
    original = DesktopImportControl.request_pause

    def request_pause(control: DesktopImportControl) -> None:
        original(control)
        pause_requested.set()

    monkeypatch.setattr(DesktopImportControl, "request_pause", request_pause)
    return pause_requested


def test_engine_admits_multiple_documents_to_analysis_concurrently(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    sources = [tmp_path / "first.md", tmp_path / "second.md"]
    for ordinal, source in enumerate(sources):
        source.write_text(f"# Document {ordinal}\n\nIndependent evidence.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    both_started = threading.Event()
    release_analysis = threading.Event()
    started_documents: set[str] = set()
    started_lock = threading.Lock()

    def analyze(request, _timeout_seconds):
        if request.operation == "knowledge_fact_harvest":
            with started_lock:
                started_documents.add(request.document_name)
                if len(started_documents) == 2:
                    both_started.set()
            assert release_analysis.wait(timeout=2)
        return _empty_knowledge_analysis()

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(analyze),
    )
    server._handshake_complete = True
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def run_import(source) -> None:
        try:
            results.append(
                server._dispatch(
                    _request(
                        source.stem, "workbench.import_text_document", source_path=str(source)
                    ),
                    cancel_event=None,
                )
            )
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=run_import, args=(source,)) for source in sources]
    for worker in workers:
        worker.start()
    assert both_started.wait(timeout=2)
    release_analysis.set()
    for worker in workers:
        worker.join(timeout=3)

    assert not errors
    assert len(results) == 2
    assert started_documents == {"first.md", "second.md"}


@pytest.mark.parametrize(
    "activation_method",
    ("workbench.open_knowledge_base", "workbench.create_knowledge_base"),
)
def test_switch_pauses_an_explicit_import_at_its_next_stage_checkpoint(
    tmp_path, monkeypatch, activation_method
) -> None:
    """A KB switch waits for one atomic Stage, not the complete Import Job."""
    first_kb = tmp_path / "first-kb"
    second_kb = tmp_path / "second-kb"
    source = tmp_path / "long-analysis.md"
    source.write_text("# Evidence\n\nA switch should preserve this import.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(first_kb, name="First")
    if activation_method == "workbench.open_knowledge_base":
        DesktopKnowledgeBaseRuntime().create(second_kb, name="Second")
    analysis_started = threading.Event()
    release_analysis = threading.Event()
    pause_requested = _observe_pause_requests(monkeypatch)

    def analyze(request, _timeout_seconds):
        if request.operation == "knowledge_fact_harvest":
            analysis_started.set()
            assert release_analysis.wait(timeout=2)
        return _empty_knowledge_analysis()

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(analyze),
    )
    server._handshake_complete = True
    import_errors: list[DesktopImportError] = []

    def run_import() -> None:
        try:
            server._dispatch(
                _request("import", "workbench.import_text_document", source_path=str(source)),
                cancel_event=None,
            )
        except DesktopImportError as error:
            import_errors.append(error)

    import_worker = threading.Thread(target=run_import)
    import_worker.start()
    assert analysis_started.wait(timeout=2)

    switch_result: dict[str, object] = {}
    switch_done = threading.Event()

    def switch_knowledge_base() -> None:
        switch_result.update(
            server._dispatch(
                _request(
                    "switch",
                    activation_method,
                    kb_dir=str(second_kb),
                    name="Second",
                ),
                cancel_event=None,
            )
        )
        switch_done.set()

    switch_worker = threading.Thread(target=switch_knowledge_base)
    switch_worker.start()
    assert pause_requested.wait(timeout=2)
    assert not switch_done.is_set()
    release_analysis.set()
    import_worker.join(timeout=2)
    switch_worker.join(timeout=2)

    assert not import_worker.is_alive()
    assert not switch_worker.is_alive()
    assert [error.code for error in import_errors] == ["import_paused"]
    assert switch_result["knowledge_base"]["kb_dir"] == str(second_kb)

    server._dispatch(
        _request("return", "workbench.open_knowledge_base", kb_dir=str(first_kb)),
        cancel_event=None,
    )
    jobs = server._dispatch(
        _request("jobs", "workbench.import_jobs"),
        cancel_event=None,
    )["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job"]["status"] == "paused"
    job_id = jobs[0]["job"]["job_id"]
    paused_stages = {stage["stage"]: stage for stage in jobs[0]["stages"]}
    completed_checkpoint_ids = {
        stage: value["stage_run_id"]
        for stage, value in paused_stages.items()
        if value["status"] == "completed"
    }
    assert completed_checkpoint_ids

    resumed = server._dispatch(
        _request("resume", "workbench.resume_import_job", job_id=job_id),
        cancel_event=None,
    )
    assert resumed["job"]["status"] == "completed"
    assert resumed["job"]["job_id"] == job_id
    assert resumed["document"]["availability"] == "available"
    resumed_stages = {stage["stage"]: stage for stage in resumed["stages"]}
    assert {
        stage: resumed_stages[stage]["stage_run_id"] for stage in completed_checkpoint_ids
    } == completed_checkpoint_ids


def test_switch_pauses_an_explicit_recovery_import_before_activation(tmp_path, monkeypatch) -> None:
    """An explicitly resumed recovery drains through the same transition seam."""
    first_kb = tmp_path / "first-kb"
    second_kb = tmp_path / "second-kb"
    source = tmp_path / "recoverable.md"
    source.write_text("# Recoverable\n\nResume this job on startup.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(first_kb, name="First")
    DesktopKnowledgeBaseRuntime().create(second_kb, name="Second")
    state = DesktopImportStore(first_kb).create_job(source)
    analysis_started = threading.Event()
    release_analysis = threading.Event()
    pause_requested = _observe_pause_requests(monkeypatch)

    def analyze(request, _timeout_seconds):
        if request.operation == "knowledge_fact_harvest":
            analysis_started.set()
            assert release_analysis.wait(timeout=2)
        return _empty_knowledge_analysis()

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(analyze),
    )
    server._handshake_complete = True
    server._dispatch(
        _request("open-first", "workbench.open_knowledge_base", kb_dir=str(first_kb)),
        cancel_event=None,
    )
    assert not analysis_started.wait(timeout=0.05)

    recovery_errors: list[DesktopImportError] = []

    def resume_import() -> None:
        try:
            server._dispatch(
                _request(
                    "resume",
                    "workbench.resume_import_job",
                    job_id=state.job_id,
                ),
                cancel_event=None,
            )
        except DesktopImportError as error:
            recovery_errors.append(error)

    recovery_worker = threading.Thread(target=resume_import)
    recovery_worker.start()
    assert analysis_started.wait(timeout=2)

    switch_done = threading.Event()

    def switch_knowledge_base() -> None:
        server._dispatch(
            _request("switch", "workbench.open_knowledge_base", kb_dir=str(second_kb)),
            cancel_event=None,
        )
        switch_done.set()

    switch_worker = threading.Thread(target=switch_knowledge_base)
    switch_worker.start()
    assert pause_requested.wait(timeout=2)
    assert not switch_done.is_set()
    release_analysis.set()
    recovery_worker.join(timeout=2)
    switch_worker.join(timeout=2)

    assert not recovery_worker.is_alive()
    assert not switch_worker.is_alive()
    assert [error.code for error in recovery_errors] == ["import_paused"]
    assert DesktopImportStore(first_kb).task(state.job_id).job.status == "paused"
    active = server._dispatch(
        _request("active", "workbench.active_knowledge_base"),
        cancel_event=None,
    )
    assert active["knowledge_base"]["kb_dir"] == str(second_kb)


def test_import_admission_rebinds_to_the_new_kb_after_transition(tmp_path, monkeypatch) -> None:
    """An import arriving after admission closes cannot retain the old KB binding."""
    first_kb = tmp_path / "first-kb"
    second_kb = tmp_path / "second-kb"
    first_source = tmp_path / "first.md"
    second_source = tmp_path / "second.md"
    first_source.write_text("# First\n\nPause this import.", encoding="utf-8")
    second_source.write_text("# Second\n\nPublish only in the new KB.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(first_kb, name="First")
    DesktopKnowledgeBaseRuntime().create(second_kb, name="Second")
    analysis_started = threading.Event()
    release_analysis = threading.Event()
    pause_requested = _observe_pause_requests(monkeypatch)

    def factory(kb_dir, _override):
        def analyze(request, _timeout_seconds):
            if (
                kb_dir.resolve() == first_kb.resolve()
                and request.operation == "knowledge_fact_harvest"
            ):
                analysis_started.set()
                assert release_analysis.wait(timeout=2)
            return _empty_knowledge_analysis()

        return DesktopModelGateway(analyze)

    server = DesktopEngineServer(
        io.BytesIO(), io.BytesIO(), workspace=workspace, model_gateway_factory=factory
    )
    server._handshake_complete = True
    first_errors: list[DesktopImportError] = []

    def import_first() -> None:
        try:
            server._dispatch(
                _request(
                    "first-import",
                    "workbench.import_text_document",
                    source_path=str(first_source),
                ),
                cancel_event=None,
            )
        except DesktopImportError as error:
            first_errors.append(error)

    first_worker = threading.Thread(target=import_first)
    first_worker.start()
    assert analysis_started.wait(timeout=2)

    switch_worker = threading.Thread(
        target=lambda: server._dispatch(
            _request("switch", "workbench.open_knowledge_base", kb_dir=str(second_kb)),
            cancel_event=None,
        )
    )
    switch_worker.start()
    assert pause_requested.wait(timeout=2)

    contender_entered = threading.Event()
    original_import_job = server._workspace_transition.import_job

    @contextmanager
    def observed_import_job(*, expected_kb_dir=None):
        contender_entered.set()
        with original_import_job(expected_kb_dir=expected_kb_dir) as lease:
            yield lease

    monkeypatch.setattr(server._workspace_transition, "import_job", observed_import_job)
    contender_results: list[dict[str, object]] = []
    contender_errors: list[BaseException] = []

    def import_contender() -> None:
        try:
            contender_results.append(
                server._dispatch(
                    _request(
                        "contender",
                        "workbench.import_text_document",
                        source_path=str(second_source),
                    ),
                    cancel_event=None,
                )
            )
        except BaseException as error:
            contender_errors.append(error)

    contender = threading.Thread(target=import_contender)
    contender.start()
    assert contender_entered.wait(timeout=2)
    release_analysis.set()
    for worker in (first_worker, switch_worker, contender):
        worker.join(timeout=3)
        assert not worker.is_alive()

    assert [error.code for error in first_errors] == ["import_paused"]
    assert contender_errors == []
    assert contender_results[0]["job"]["status"] == "completed"
    document_id = contender_results[0]["document"]["document_id"]
    with sqlite3.connect(first_kb / ".openkb" / "state.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM source_documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            is None
        )
    with sqlite3.connect(second_kb / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?", (document_id,)
        ).fetchone() == ("available",)


def test_failed_switch_reopens_import_admission_on_the_old_kb(tmp_path, monkeypatch) -> None:
    first_kb = tmp_path / "first-kb"
    missing_kb = tmp_path / "missing-kb"
    first_source = tmp_path / "first.md"
    next_source = tmp_path / "next.md"
    first_source.write_text("# First\n\nPause before the failed switch.", encoding="utf-8")
    next_source.write_text("# Next\n\nAdmission opens again.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(first_kb, name="First")
    analysis_started = threading.Event()
    release_analysis = threading.Event()
    pause_requested = _observe_pause_requests(monkeypatch)
    analysis_calls = 0

    def analyze(request, _timeout_seconds):
        nonlocal analysis_calls
        if request.operation == "knowledge_fact_harvest":
            analysis_calls += 1
            if analysis_calls == 1:
                analysis_started.set()
                assert release_analysis.wait(timeout=2)
        return _empty_knowledge_analysis()

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(analyze),
    )
    server._handshake_complete = True
    import_errors: list[DesktopImportError] = []
    switch_errors: list[DesktopKnowledgeBaseError] = []

    def run_first_import() -> None:
        try:
            server._dispatch(
                _request("first", "workbench.import_text_document", source_path=str(first_source)),
                cancel_event=None,
            )
        except DesktopImportError as error:
            import_errors.append(error)

    first_worker = threading.Thread(target=run_first_import)
    first_worker.start()
    assert analysis_started.wait(timeout=2)

    def fail_switch() -> None:
        try:
            server._dispatch(
                _request("switch", "workbench.open_knowledge_base", kb_dir=str(missing_kb)),
                cancel_event=None,
            )
        except DesktopKnowledgeBaseError as error:
            switch_errors.append(error)

    switch_worker = threading.Thread(target=fail_switch)
    switch_worker.start()
    assert pause_requested.wait(timeout=2)
    release_analysis.set()
    first_worker.join(timeout=2)
    switch_worker.join(timeout=2)
    assert not first_worker.is_alive() and not switch_worker.is_alive()
    assert [error.code for error in import_errors] == ["import_paused"]
    assert [error.code for error in switch_errors] == ["desktop_knowledge_base_not_found"]

    next_results: list[dict[str, object]] = []
    next_worker = threading.Thread(
        target=lambda: next_results.append(
            server._dispatch(
                _request("next", "workbench.import_text_document", source_path=str(next_source)),
                cancel_event=None,
            )
        ),
        daemon=True,
    )
    next_worker.start()
    next_worker.join(timeout=3)
    assert not next_worker.is_alive()
    assert next_results[0]["job"]["status"] == "completed"
    assert workspace.active().kb_dir == str(first_kb)


def test_post_activation_failure_restores_old_binding_and_admission(tmp_path, monkeypatch) -> None:
    first_kb = tmp_path / "first-kb"
    second_kb = tmp_path / "second-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(first_kb, name="First")
    DesktopKnowledgeBaseRuntime().create(second_kb, name="Second")
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    def fail_projection(kb_dir) -> None:
        if kb_dir.resolve() == second_kb.resolve():
            raise RuntimeError("injected activation failure")

    monkeypatch.setattr(
        workspace_activation_engine,
        "materialize_okf_projection",
        fail_projection,
    )

    with pytest.raises(RuntimeError, match="injected activation failure"):
        server._dispatch(
            _request("switch", "workbench.open_knowledge_base", kb_dir=str(second_kb)),
            cancel_event=None,
        )

    active = workspace.active()
    assert active is not None and active.kb_dir == str(first_kb)
    with server._workspace_transition.import_job(expected_kb_dir=first_kb) as lease:
        assert lease is not None and lease.kb_dir == first_kb.resolve()


@pytest.mark.parametrize("already_active", (False, True))
def test_every_activation_closes_admission_before_binding_is_evaluated(
    tmp_path, already_active
) -> None:
    kb_dir = tmp_path / "kb"
    workspace = DesktopKnowledgeBaseRuntime()
    if already_active:
        workspace.create(kb_dir)
    coordinator = DesktopWorkspaceTransitionCoordinator(workspace)

    with coordinator.activation(kb_dir):
        assert coordinator._transitioning is True


def test_user_cancel_wins_when_it_races_with_switch_pause(tmp_path, monkeypatch) -> None:
    first_kb = tmp_path / "first-kb"
    second_kb = tmp_path / "second-kb"
    source = tmp_path / "cancel.md"
    source.write_text("# Cancel\n\nCancellation wins.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(first_kb, name="First")
    DesktopKnowledgeBaseRuntime().create(second_kb, name="Second")
    analysis_started = threading.Event()
    release_analysis = threading.Event()
    pause_requested = _observe_pause_requests(monkeypatch)

    def analyze(request, _timeout_seconds):
        if request.operation == "knowledge_fact_harvest":
            analysis_started.set()
            assert release_analysis.wait(timeout=2)
        return _empty_knowledge_analysis()

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(analyze),
    )
    server._handshake_complete = True
    import_errors: list[DesktopImportError] = []

    def run_import() -> None:
        try:
            server._dispatch(
                _request("import", "workbench.import_text_document", source_path=str(source)),
                cancel_event=None,
            )
        except DesktopImportError as error:
            import_errors.append(error)

    import_worker = threading.Thread(target=run_import)
    import_worker.start()
    assert analysis_started.wait(timeout=2)
    job_id = DesktopImportStore(first_kb).list_import_jobs()["jobs"][0]["job"]["job_id"]
    switch_worker = threading.Thread(
        target=lambda: server._dispatch(
            _request("switch", "workbench.open_knowledge_base", kb_dir=str(second_kb)),
            cancel_event=None,
        )
    )
    switch_worker.start()
    assert pause_requested.wait(timeout=2)
    server._dispatch(
        _request("cancel", "workbench.cancel_import_job", job_id=job_id),
        cancel_event=None,
    )
    release_analysis.set()
    import_worker.join(timeout=2)
    switch_worker.join(timeout=2)
    assert not import_worker.is_alive() and not switch_worker.is_alive()
    assert [error.code for error in import_errors] == ["import_interrupted"]
    assert DesktopImportStore(first_kb).task(job_id).job.status == "paused"


def test_reopening_the_same_kb_does_not_pause_its_import(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "kb"
    source = tmp_path / "same.md"
    source.write_text("# Same\n\nKeep running.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    analysis_started = threading.Event()
    release_analysis = threading.Event()
    pause_requested = _observe_pause_requests(monkeypatch)

    def analyze(request, _timeout_seconds):
        if request.operation == "knowledge_fact_harvest":
            analysis_started.set()
            assert release_analysis.wait(timeout=2)
        return _empty_knowledge_analysis()

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(analyze),
    )
    server._handshake_complete = True
    import_results: list[dict[str, object]] = []
    import_worker = threading.Thread(
        target=lambda: import_results.append(
            server._dispatch(
                _request("import", "workbench.import_text_document", source_path=str(source)),
                cancel_event=None,
            )
        )
    )
    import_worker.start()
    assert analysis_started.wait(timeout=2)

    activation_entered = threading.Event()
    original_activation = server._workspace_transition.activation

    @contextmanager
    def observed_activation(target_kb_dir):
        with original_activation(target_kb_dir):
            activation_entered.set()
            yield

    monkeypatch.setattr(server._workspace_transition, "activation", observed_activation)
    reopen_worker = threading.Thread(
        target=lambda: server._dispatch(
            _request("reopen", "workbench.open_knowledge_base", kb_dir=str(kb_dir)),
            cancel_event=None,
        )
    )
    reopen_worker.start()
    assert activation_entered.wait(timeout=2)
    assert not pause_requested.is_set()
    release_analysis.set()
    import_worker.join(timeout=2)
    reopen_worker.join(timeout=2)
    assert not import_worker.is_alive() and not reopen_worker.is_alive()
    assert import_results[0]["job"]["status"] == "completed"
