"""Optional PageTree summaries degrade without changing deterministic evidence."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import time

from openkb import desktop_engine_page_tree_enrichment as enrichment_engine
from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_model_settings import read_desktop_model_settings
from openkb.desktop_page_tree_enrichment import DesktopPageTreeEnrichmentService
from openkb.desktop_page_tree_rebuild_state import queue_page_tree_rebuild_in
from openkb.desktop_page_tree_store import load_current_page_tree_in
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _gateway(provider: str, model: str, summary: str) -> DesktopModelGateway:
    def transport(request, _timeout):
        payload = json.loads(request.content)
        target = next(node for node in payload["nodes"] if node["evidence"])
        return json.dumps(
            {
                "schema_version": "openkb.page-tree-enrichment.v1",
                "summaries": [{"node_id": target["node_id"], "summary": summary}],
            }
        )

    return DesktopModelGateway(transport, provider_name=provider, model_name=model)


def test_enrichment_activates_only_summary_overlay_and_keeps_evidence_authoritative(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text("# Routing\n\nUse lexical evidence for answers.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        base_rows = connection.execute(
            "SELECT node_id, parent_node_id, node_order, locator_json, summary "
            "FROM document_page_tree_nodes ORDER BY node_order"
        ).fetchall()
        evidence_before = connection.execute(
            "SELECT evidence_id, text FROM evidence_refs ORDER BY ordinal"
        ).fetchall()
        assert all(row[4] is None for row in base_rows)

    gateway = _gateway("provider-a", "model-a", "Routes questions to lexical evidence.")
    service = DesktopPageTreeEnrichmentService(kb_dir)
    assert service.queue_eligible(gateway) == 1
    assert service.run_document(imported.document.document_id, gateway, should_stop=lambda: False)

    with sqlite3.connect(database_path) as connection:
        tree = load_current_page_tree_in(connection, imported.document.document_id)
        assert tree is not None
        assert any(node.summary == "Routes questions to lexical evidence." for node in tree.nodes)
        assert (
            connection.execute(
                "SELECT node_id, parent_node_id, node_order, locator_json, summary "
                "FROM document_page_tree_nodes ORDER BY node_order"
            ).fetchall()
            == base_rows
        )
        assert (
            connection.execute(
                "SELECT evidence_id, text FROM evidence_refs ORDER BY ordinal"
            ).fetchall()
            == evidence_before
        )
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("available",)
        assert connection.execute(
            "SELECT status FROM document_page_tree_enrichment_tasks WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("completed",)
    task = DesktopTextImportService(kb_dir).list_import_jobs()["page_tree_enrichments"][0]
    assert task["status"] == "completed"
    assert task["provider"] == "provider-a"


def test_enrichment_failure_is_task_only_and_deterministic_tree_stays_available(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "failure.md"
    source.write_text("# Stable\n\nThe base tree remains usable.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)

    def timeout(_request, _timeout):
        raise TimeoutError()

    gateway = DesktopModelGateway(timeout, provider_name="provider-a", model_name="model-a")
    service = DesktopPageTreeEnrichmentService(kb_dir)
    assert service.queue_eligible(gateway) == 1
    assert not service.run_document(
        imported.document.document_id, gateway, should_stop=lambda: False
    )

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        tree = load_current_page_tree_in(connection, imported.document.document_id)
        assert tree is not None
        assert all(node.summary is None for node in tree.nodes)
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("available",)
        assert connection.execute(
            "SELECT status, error_code, attempt_count, model_attempt "
            "FROM document_page_tree_enrichment_tasks WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("failed", "model_timeout", 1, 4)
        assert connection.execute("SELECT COUNT(*) FROM quarantined_documents").fetchone() == (0,)


def test_model_change_creates_a_new_enrichment_generation_without_overwriting_base(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "rerun.md"
    source.write_text("# Versioned\n\nSummary overlays are immutable.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    service = DesktopPageTreeEnrichmentService(kb_dir)

    first = _gateway("provider-a", "model-a", "First routing summary.")
    assert service.queue_eligible(first) == 1
    assert service.run_document(imported.document.document_id, first, should_stop=lambda: False)
    second = _gateway("provider-b", "model-b", "Second routing summary.")
    assert service.queue_eligible(second) == 1
    assert service.run_document(imported.document.document_id, second, should_stop=lambda: False)

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT provider, model, status FROM document_page_tree_enrichment_generations "
            "ORDER BY created_at"
        ).fetchall() == [
            ("provider-a", "model-a", "superseded"),
            ("provider-b", "model-b", "current"),
        ]
        tree = load_current_page_tree_in(connection, imported.document.document_id)
        assert tree is not None
        assert any(node.summary == "Second routing summary." for node in tree.nodes)
        assert connection.execute(
            "SELECT COUNT(*) FROM document_page_tree_generations"
        ).fetchone() == (1,)


def test_current_overlay_repairs_stale_task_and_settings_retry_failed_target(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "retry.md"
    source.write_text("# Retry\n\nThe current summary remains usable.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    service = DesktopPageTreeEnrichmentService(kb_dir)
    current = _gateway("provider-a", "model-a", "Current summary.")
    assert service.queue_eligible(current) == 1
    assert service.run_document(imported.document.document_id, current, should_stop=lambda: False)
    assert service.queue_eligible(current) == 0
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT attempt_count FROM document_page_tree_enrichment_tasks"
        ).fetchone() == (1,)

    failed = DesktopModelGateway(
        lambda _request, _timeout: (_ for _ in ()).throw(TimeoutError()),
        provider_name="provider-b",
        model_name="model-b",
    )
    assert service.queue_eligible(failed) == 1
    assert not service.run_document(
        imported.document.document_id, failed, should_stop=lambda: False
    )
    assert service.queue_eligible(current) == 0
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT status, provider, model, error_code FROM document_page_tree_enrichment_tasks"
        ).fetchone() == ("completed", "provider-a", "model-a", None)

    assert service.queue_eligible(failed) == 1
    assert not service.run_document(
        imported.document.document_id, failed, should_stop=lambda: False
    )
    assert service.queue_eligible(failed) == 0
    assert service.queue_eligible(failed, retry_failed=True) == 1


def test_recovered_task_rejects_a_late_model_result(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "restart.md"
    source.write_text("# Restart\n\nLate results must not become current.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    started = threading.Event()
    release = threading.Event()

    def transport(request, _timeout):
        payload = json.loads(request.content)
        target = next(node for node in payload["nodes"] if node["evidence"])
        started.set()
        assert release.wait(timeout=1)
        return json.dumps(
            {
                "schema_version": "openkb.page-tree-enrichment.v1",
                "summaries": [{"node_id": target["node_id"], "summary": "Late summary."}],
            }
        )

    gateway = DesktopModelGateway(transport, provider_name="provider-a", model_name="model-a")
    service = DesktopPageTreeEnrichmentService(kb_dir)
    service.queue_eligible(gateway)
    worker = threading.Thread(
        target=service.run_document,
        args=(imported.document.document_id, gateway),
        kwargs={"should_stop": lambda: False},
    )
    worker.start()
    assert started.wait(timeout=1)
    assert service.recover_interrupted() == 1
    release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM document_page_tree_enrichment_tasks"
        ).fetchone() == ("pending",)
        assert connection.execute(
            "SELECT COUNT(*) FROM document_page_tree_enrichment_generations"
        ).fetchone() == (0,)


def test_unavailable_document_terminalizes_queued_and_running_enrichment(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "unavailable.md"
    source.write_text("# Availability\n\nOnly Available documents are enriched.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    service = DesktopPageTreeEnrichmentService(kb_dir)
    started = threading.Event()
    release = threading.Event()

    def transport(request, _timeout):
        payload = json.loads(request.content)
        target = next(node for node in payload["nodes"] if node["evidence"])
        started.set()
        assert release.wait(timeout=1)
        return json.dumps(
            {
                "schema_version": "openkb.page-tree-enrichment.v1",
                "summaries": [{"node_id": target["node_id"], "summary": "Too late."}],
            }
        )

    gateway = DesktopModelGateway(transport, provider_name="provider-a", model_name="model-a")
    assert service.queue_eligible(gateway) == 1
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (imported.document.document_id,),
        )
        connection.commit()
    assert service.pending_document_ids(gateway) == ()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status, error_code FROM document_page_tree_enrichment_tasks"
        ).fetchone() == ("failed", "source_document_unavailable")
        connection.execute(
            "UPDATE source_documents SET availability = 'available' WHERE document_id = ?",
            (imported.document.document_id,),
        )
        connection.commit()
    assert service.queue_eligible(gateway) == 1

    worker = threading.Thread(
        target=service.run_document,
        args=(imported.document.document_id, gateway),
        kwargs={"should_stop": lambda: False},
    )
    worker.start()
    assert started.wait(timeout=1)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (imported.document.document_id,),
        )
        connection.commit()
    release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status, error_code FROM document_page_tree_enrichment_tasks"
        ).fetchone() == ("failed", "source_document_unavailable")
        assert connection.execute(
            "SELECT COUNT(*) FROM document_page_tree_enrichment_generations"
        ).fetchone() == (0,)


def test_desktop_import_starts_engine_owned_enrichment_after_document_publication(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "engine.md"
    source.write_text("# Engine\n\nStart enrichment after publication.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)

    def transport(request, _timeout):
        if request.operation == "knowledge_analysis":
            return json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "document",
                    "document_description": "No durable knowledge candidates.",
                    "concepts": [],
                    "entities": [],
                }
            )
        if request.operation == "page_tree_enrichment":
            payload = json.loads(request.content)
            target = next(node for node in payload["nodes"] if node["evidence"])
            return json.dumps(
                {
                    "schema_version": "openkb.page-tree-enrichment.v1",
                    "summaries": [
                        {"node_id": target["node_id"], "summary": "Engine-owned summary."}
                    ],
                }
            )
        return json.dumps({"nodes": [], "edges": []})

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            transport, provider_name="provider-a", model_name="model-a"
        ),
    )
    server._handshake_complete = True
    server._dispatch(
        DesktopRequest(
            request_id="import",
            method="workbench.import_text_document",
            params={"source_path": str(source)},
        ),
        cancel_event=None,
    )

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    deadline = time.monotonic() + 2
    status = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT status FROM document_page_tree_enrichment_tasks"
            ).fetchone()
        status = row[0] if row is not None else None
        if status == "completed":
            break
        time.sleep(0.01)
    server._shutdown.set()
    server._join_workers()
    assert status == "completed"


def test_settings_save_rejects_old_result_and_restarts_with_live_gateway(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "settings.md"
    source.write_text("# Settings\n\nOnly the current provider may publish.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    started = threading.Event()
    release = threading.Event()

    def old_transport(request, _timeout):
        payload = json.loads(request.content)
        target = next(node for node in payload["nodes"] if node["evidence"])
        started.set()
        assert release.wait(timeout=2)
        return json.dumps(
            {
                "schema_version": "openkb.page-tree-enrichment.v1",
                "summaries": [{"node_id": target["node_id"], "summary": "Old summary."}],
            }
        )

    def live_factory(path, _override):
        settings = read_desktop_model_settings(path)
        return _gateway(settings.provider, settings.model, "New summary.")

    server = DesktopEngineServer(
        io.BytesIO(), io.BytesIO(), workspace=workspace, model_gateway_factory=live_factory
    )
    server._handshake_complete = True
    enrichment_engine.start_page_tree_enrichments(
        server,
        kb_dir,
        DesktopModelGateway(old_transport, provider_name="provider-old", model_name="model-old"),
    )
    assert started.wait(timeout=1)
    saved = server._dispatch(
        DesktopRequest(
            request_id="save",
            method="workbench.save_model_settings",
            params={
                "provider": "deepseek",
                "model": "model-new",
                "api_base_url": "https://models.example.test/v1",
                "api_key": "test-key",
                "max_concurrent_model_calls": 1,
                "initial_timeout_seconds": 20,
            },
        ),
        cancel_event=None,
    )
    assert saved["model"] == "model-new"
    release.set()
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    deadline = time.monotonic() + 2
    current = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path) as connection:
            current = connection.execute(
                "SELECT provider, model FROM document_page_tree_enrichment_generations "
                "WHERE status = 'current'"
            ).fetchone()
        if current == ("deepseek", "model-new"):
            break
        time.sleep(0.01)
    server._shutdown.set()
    server._join_workers()
    assert current == ("deepseek", "model-new")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM document_page_tree_enrichment_generations "
            "WHERE provider = 'provider-old'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT document_id FROM document_page_tree_enrichment_current"
        ).fetchone() == (imported.document.document_id,)


def test_enrichment_worker_waits_for_deterministic_rebuild(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "priority.md"
    source.write_text("# Priority\n\nDeterministic work runs first.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        queue_page_tree_rebuild_in(
            connection,
            imported.document.document_id,
            reason="provider_update",
            error_code="provider_update",
            provider_version="future",
        )
        connection.commit()
    called = threading.Event()

    def transport(_request, _timeout):
        called.set()
        raise AssertionError("low-priority enrichment ran before deterministic rebuild")

    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    enrichment_engine.start_page_tree_enrichments(
        server,
        kb_dir,
        DesktopModelGateway(transport, provider_name="provider-a", model_name="model-a"),
    )
    assert not called.wait(timeout=0.25)
    server._shutdown.set()
    server._join_workers()


def test_worker_start_failure_is_best_effort_and_malformed_config_disables_enrichment(
    tmp_path, monkeypatch
):
    kb_dir = tmp_path / "knowledge"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)

    def fail_start(_thread):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    enrichment_engine.start_page_tree_enrichments(
        server, kb_dir, _gateway("provider-a", "model-a", "Summary.")
    )
    assert server._workers == set()
    assert server._page_tree_enrichment_workers == set()
    assert server._page_tree_enrichment_gateways == {}

    (kb_dir / ".openkb" / "config.yaml").write_text("desktop: [unterminated", encoding="utf-8")
    enrichment_engine.start_page_tree_enrichments(
        server, kb_dir, _gateway("provider-a", "model-a", "Summary.")
    )
    assert server._page_tree_enrichment_workers == set()


def test_inactive_knowledge_base_cannot_start_enrichment_worker(tmp_path):
    inactive_kb = tmp_path / "inactive"
    active_kb = tmp_path / "active"
    DesktopKnowledgeBaseRuntime().create(inactive_kb)
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(active_kb)
    called = threading.Event()
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    enrichment_engine.start_page_tree_enrichments(
        server,
        inactive_kb,
        DesktopModelGateway(
            lambda _request, _timeout: called.set(),
            provider_name="provider-a",
            model_name="model-a",
        ),
    )
    assert not called.is_set()
    assert server._page_tree_enrichment_workers == set()
