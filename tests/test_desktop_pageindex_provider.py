"""Experimental official PageIndex adapter and fixed-evaluation boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import openkb.desktop_pageindex_provider as pageindex_provider
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_import_artifacts import DocumentIRBlock, build_evidence
from openkb.desktop_page_tree import DETERMINISTIC_PROVIDER_KIND
from openkb.desktop_pageindex_acceptance import (
    pageindex_evaluation_corpus_identity,
    run_pageindex_package_acceptance,
    validate_pageindex_evaluation,
)
from openkb.desktop_pageindex_adapter import (
    PAGEINDEX_PROVIDER_KIND,
    PAGEINDEX_PROVIDER_VERSION,
    PageIndexProviderError,
    _subprocess_invoker,
    build_official_pageindex_generation,
)
from openkb.desktop_pageindex_provider import (
    PAGEINDEX_CACHE_SCHEMA,
    PageIndexEvaluationProvider,
    materialize_official_pageindex_provider,
)
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_evaluation import DesktopRetrievalEvaluator
from openkb.desktop_retrieval_evaluation_types import DesktopRetrievalEvaluationSuite
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_dir
from openkb.locks import kb_ingest_lock, kb_ingest_lock_held


def _blocks() -> tuple[DocumentIRBlock, ...]:
    return (
        DocumentIRBlock("b0", 0, "heading", "Guide", ("Guide",), 1, 1),
        DocumentIRBlock("b1", 1, "paragraph", "Alpha fact.", ("Guide",), 3, 3),
        DocumentIRBlock("b2", 2, "heading", "Detail", ("Guide", "Detail"), 5, 5),
        DocumentIRBlock("b3", 3, "paragraph", "Beta fact.", ("Guide", "Detail"), 7, 7),
    )


def _pageindex_result(input_path: Path, output_path: Path, _timeout: float) -> None:
    markdown = input_path.read_text(encoding="utf-8")
    assert "Alpha fact." in markdown
    output_path.write_text(
        json.dumps(
            {
                "doc_name": "document",
                "line_count": markdown.count("\n") + 1,
                "structure": [
                    {
                        "title": "Guide",
                        "node_id": "0001",
                        "line_num": 1,
                        "nodes": [
                            {
                                "title": "Detail",
                                "node_id": "0002",
                                "line_num": 5,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _package_acceptance_result(input_path: Path, output_path: Path, _timeout: float) -> None:
    markdown = input_path.read_text(encoding="utf-8")
    output_path.write_text(
        json.dumps(
            {
                "line_count": markdown.count("\n") + 1,
                "structure": [{"title": "Portable PageIndex", "node_id": "0001", "line_num": 1}],
            }
        ),
        encoding="utf-8",
    )


def test_subprocess_invoker_preserves_virtual_environment_launcher(tmp_path, monkeypatch) -> None:
    input_path = tmp_path / "document.md"
    output_path = tmp_path / "tree.json"
    input_path.write_text("# Guide\n", encoding="utf-8")
    launcher = Path("isolated-runtime/bin/python")
    captured: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        captured.append(tuple(command))
        output_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(Path, "resolve", lambda _self: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr("openkb.desktop_pageindex_adapter.subprocess.run", run)

    _subprocess_invoker(launcher)(input_path, output_path, 1.0)

    assert captured[0][0] == os.path.abspath(launcher)
    assert captured[0][1].endswith("desktop_pageindex_worker.py")


def test_subprocess_invoker_runs_a_frozen_worker_directly(tmp_path, monkeypatch) -> None:
    input_path = tmp_path / "document.md"
    output_path = tmp_path / "tree.json"
    input_path.write_text("# Guide\n", encoding="utf-8")
    worker = Path("portable/runtime/pageindex/OpenKBPageIndex.exe")
    captured: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        captured.append(tuple(command))
        output_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("openkb.desktop_pageindex_adapter.subprocess.run", run)

    _subprocess_invoker(None, worker)(input_path, output_path, 1.0)

    assert captured == [
        (os.path.abspath(worker), str(input_path), str(output_path)),
    ]


def test_subprocess_invoker_rejects_two_runtime_kinds() -> None:
    with pytest.raises(ValueError):
        _subprocess_invoker(Path("python"), Path("OpenKBPageIndex.exe"))


def test_adapter_normalizes_pageindex_nodes_without_owning_kb_cache(tmp_path) -> None:
    blocks = _blocks()
    evidence = build_evidence(blocks)
    calls = 0

    def invoke(input_path: Path, output_path: Path, timeout: float) -> None:
        nonlocal calls
        calls += 1
        _pageindex_result(input_path, output_path, timeout)

    generation = build_official_pageindex_generation(
        "document-1", blocks, evidence, (), invoke=invoke
    )
    assert generation.provider_kind == PAGEINDEX_PROVIDER_KIND
    assert generation.provider_version == PAGEINDEX_PROVIDER_VERSION
    assert [node.title for node in generation.nodes] == ["Document", "Guide", "Detail"]
    assert {binding.evidence_id for node in generation.nodes for binding in node.evidence} == {
        evidence_id for evidence_id, _block in evidence
    }
    assert calls == 1
    assert tuple(tmp_path.rglob("*.json")) == ()


@pytest.mark.parametrize(
    ("invoke", "error_code"),
    (
        (
            lambda input_path, output_path, timeout: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("pageindex", timeout)
            ),
            "pageindex_provider_timeout",
        ),
        (
            lambda _input_path, output_path, _timeout: output_path.write_text(
                "{}", encoding="utf-8"
            ),
            "pageindex_provider_invalid_tree",
        ),
    ),
)
def test_adapter_contains_timeout_and_invalid_tree(tmp_path, invoke, error_code) -> None:
    blocks = _blocks()
    with pytest.raises(PageIndexProviderError) as captured:
        build_official_pageindex_generation(
            "document-1",
            blocks,
            build_evidence(blocks),
            (),
            invoke=invoke,
        )
    assert captured.value.code == error_code


def test_adapter_bounds_provider_tree_depth_without_recursion(tmp_path) -> None:
    blocks = _blocks()

    def deeply_nested(input_path: Path, output_path: Path, _timeout: float) -> None:
        node: dict[str, object] = {"title": "Leaf", "node_id": "leaf", "line_num": 1}
        for depth in range(70):
            node = {
                "title": f"Level {depth}",
                "node_id": f"level-{depth}",
                "line_num": 1,
                "nodes": [node],
            }
        markdown = input_path.read_text(encoding="utf-8")
        output_path.write_text(
            json.dumps(
                {
                    "line_count": markdown.count("\n") + 1,
                    "structure": [node],
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(PageIndexProviderError) as captured:
        build_official_pageindex_generation(
            "document-1",
            blocks,
            build_evidence(blocks),
            (),
            invoke=deeply_nested,
        )
    assert captured.value.code == "pageindex_provider_invalid_tree"


def test_adapter_rejects_nonfinite_timeout_before_invocation(tmp_path) -> None:
    blocks = _blocks()
    with pytest.raises(ValueError):
        build_official_pageindex_generation(
            "document-1",
            blocks,
            build_evidence(blocks),
            (),
            timeout_seconds=float("inf"),
            invoke=lambda *_args: (_ for _ in ()).throw(AssertionError("provider invoked")),
        )


def test_evaluation_provider_keeps_default_current_and_rebuilds_from_sqlite(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nAlpha fact.\n\n## Detail\n\nBeta fact.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document

    provider = materialize_official_pageindex_provider(
        kb_dir,
        python_executable=None,
        invoke=_pageindex_result,
    )
    assert provider.degradations == ()
    assert provider.generations[0].provider_kind == PAGEINDEX_PROVIDER_KIND
    assert provider.generations[0].base_generation_id is not None
    with provider.lease(kb_dir, document.document_id) as tree:
        assert tree is not None and tree.provider_kind == PAGEINDEX_PROVIDER_KIND

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        default_provider = connection.execute(
            """
            SELECT generations.provider_kind
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE current.document_id = ?
            """,
            (document.document_id,),
        ).fetchone()
        experimental = connection.execute(
            "SELECT provider_kind, provider_version FROM document_page_tree_provider_current"
        ).fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert default_provider == (DETERMINISTIC_PROVIDER_KIND,)
    assert experimental == (PAGEINDEX_PROVIDER_KIND, PAGEINDEX_PROVIDER_VERSION)

    calls = 0

    def rebuild(input_path: Path, output_path: Path, timeout: float) -> None:
        nonlocal calls
        calls += 1
        _pageindex_result(input_path, output_path, timeout)
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        payload["structure"][0]["title"] = "Rebuilt Guide"
        output_path.write_text(json.dumps(payload), encoding="utf-8")

    rebuilt = materialize_official_pageindex_provider(
        kb_dir,
        python_executable=None,
        force_rebuild=True,
        invoke=rebuild,
    )
    assert calls == 1
    assert rebuilt.generations != provider.generations
    with rebuilt.lease(kb_dir, document.document_id) as tree:
        assert tree is not None
        assert tree.nodes[1].title == "Rebuilt Guide"

    cache_root = kb_dir / ".openkb" / "provider-cache"
    for cache_file in cache_root.rglob("*.json"):
        cache_file.unlink()
    materialize_official_pageindex_provider(
        kb_dir,
        python_executable=None,
        force_rebuild=True,
        invoke=rebuild,
    )
    assert calls == 2


def test_provider_discards_a_result_when_its_document_version_changes(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nAlpha fact.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    service = DesktopTextImportService(kb_dir)
    first = service.import_text(source).document
    worker_started = threading.Event()
    release_worker = threading.Event()
    completed: list[PageIndexEvaluationProvider] = []

    def blocked_provider(input_path: Path, output_path: Path, timeout: float) -> None:
        assert not kb_ingest_lock_held(desktop_state_dir(kb_dir))
        worker_started.set()
        assert release_worker.wait(timeout=2)
        _pageindex_result(input_path, output_path, timeout)

    worker = threading.Thread(
        target=lambda: completed.append(
            materialize_official_pageindex_provider(kb_dir, invoke=blocked_provider)
        )
    )
    worker.start()
    assert worker_started.wait(timeout=2)

    source.write_text("# Guide\n\nSecond version evidence.\n", encoding="utf-8")
    second = service.import_text(source).document
    with kb_ingest_lock(desktop_state_dir(kb_dir)):
        with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
            connection.execute(
                "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
                (first.document_id,),
            )

    release_worker.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    (provider,) = completed
    assert provider.degradations == ("pageindex_provider_result_stale",)
    assert provider.generations[0].base_generation_id is None

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT generation_id FROM document_page_tree_provider_current "
                "WHERE document_id = ?",
                (first.document_id,),
            ).fetchone()
            is None
        )
        default_kind = connection.execute(
            """
            SELECT generations.provider_kind
            FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE current.document_id = ?
            """,
            (first.document_id,),
        ).fetchone()
    assert default_kind == (DETERMINISTIC_PROVIDER_KIND,)
    assert (
        DesktopEvidenceRetriever(kb_dir).retrieve("Second version evidence").evidence[0].document_id
        == second.document_id
    )


def test_concurrent_materializations_leave_valid_cache_and_current_generation(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nAlpha fact.\n\n## Detail\n\nBeta fact.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    worker_barrier = threading.Barrier(2)
    providers: list[PageIndexEvaluationProvider] = []

    def concurrent_provider(input_path: Path, output_path: Path, timeout: float) -> None:
        assert not kb_ingest_lock_held(desktop_state_dir(kb_dir))
        worker_barrier.wait(timeout=2)
        _pageindex_result(input_path, output_path, timeout)

    workers = [
        threading.Thread(
            target=lambda: providers.append(
                materialize_official_pageindex_provider(
                    kb_dir,
                    force_rebuild=True,
                    invoke=concurrent_provider,
                )
            )
        )
        for _index in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)
        assert not worker.is_alive()

    assert len(providers) == 2
    cache_files = tuple((kb_dir / ".openkb" / "provider-cache").rglob("*.json"))
    assert len(cache_files) == 1
    cache = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert cache["schema_version"] == PAGEINDEX_CACHE_SCHEMA
    stored_digest = cache["checkpoint_sha256"]
    assert (
        stored_digest
        == hashlib.sha256(
            json.dumps(
                cache["checkpoint"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        (current_generation_id,) = connection.execute(
            "SELECT generation_id FROM document_page_tree_provider_current WHERE document_id = ?",
            (document.document_id,),
        ).fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    reused = materialize_official_pageindex_provider(
        kb_dir,
        invoke=lambda *_args: (_ for _ in ()).throw(AssertionError("provider invoked")),
    )
    assert reused.generations[0].base_generation_id == current_generation_id


def test_cache_survives_a_database_publication_failure(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nAlpha fact.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    original_store = pageindex_provider.store_page_tree_generation_in
    attempts = 0

    def fail_first_publication(connection, document_id, generation) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("injected publication failure")
        original_store(connection, document_id, generation)

    monkeypatch.setattr(pageindex_provider, "store_page_tree_generation_in", fail_first_publication)
    failed = materialize_official_pageindex_provider(kb_dir, invoke=_pageindex_result)
    assert failed.generations[0].base_generation_id is None
    assert tuple((kb_dir / ".openkb" / "provider-cache").rglob("*.json"))

    recovered = materialize_official_pageindex_provider(
        kb_dir,
        invoke=lambda *_args: (_ for _ in ()).throw(AssertionError("provider invoked")),
    )
    assert recovered.generations[0].document_id == document.document_id
    assert recovered.generations[0].base_generation_id is not None
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_provider_uses_published_evidence_ids_for_d1_and_d2_versions(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "first.md"
    second_source = tmp_path / "second.md"
    third_source = tmp_path / "third.md"
    first_source.write_bytes(b"# Guide\n\nAlpha fact.\n\n## Detail\n\nBeta fact.\n")
    second_source.write_bytes(b"# Guide\r\n\r\nAlpha fact.\r\n\r\n## Detail\r\n\r\nBeta fact.\r\n")
    third_source.write_bytes(b"# Guide\n\nAlpha fact.\n\n## Extra\n\nGamma fact.\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    service = DesktopTextImportService(kb_dir)
    first = service.import_text(first_source).document
    second_result = service.import_text(second_source)
    assert second_result.job.deduplication is not None
    assert second_result.job.deduplication.level == "D1"
    third_result = service.import_text(third_source)
    assert third_result.job.deduplication is not None
    assert third_result.job.deduplication.level == "D2"

    provider = materialize_official_pageindex_provider(
        kb_dir,
        python_executable=None,
        invoke=_pageindex_result,
    )
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        for document_id in (
            first.document_id,
            second_result.document.document_id,
            third_result.document.document_id,
        ):
            expected = {
                str(row[0])
                for row in connection.execute(
                    "SELECT evidence_id FROM evidence_occurrences WHERE document_id = ?",
                    (document_id,),
                ).fetchall()
            }
            with provider.lease(kb_dir, document_id) as tree:
                assert tree is not None
                actual = {binding.evidence_id for node in tree.nodes for binding in node.evidence}
            assert actual == expected


def test_provider_failure_is_reported_without_changing_available_baseline(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nAlpha baseline evidence.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    def failed(_input: Path, _output: Path, _timeout: float) -> None:
        raise OSError("provider unavailable")

    provider = materialize_official_pageindex_provider(
        kb_dir,
        python_executable=None,
        invoke=failed,
    )
    assert provider.degradations == ("pageindex_provider_unavailable",)
    assert provider.generations[0].base_generation_id is None
    assert DesktopEvidenceRetriever(kb_dir).retrieve("Alpha baseline").evidence
    snapshot = DesktopRetrievalEvaluator(kb_dir, page_tree_provider=provider)._derived_snapshot()
    assert snapshot.page_tree_providers[0].provider_kind == PAGEINDEX_PROVIDER_KIND
    assert not snapshot.identity_bound


def test_packaged_acceptance_contains_provider_failures_without_changing_kb(
    tmp_path, caplog
) -> None:
    worker = tmp_path / "OpenKBPageIndex.exe"
    worker.write_bytes(b"test placeholder")

    def timeout(_input: Path, _output: Path, seconds: float) -> None:
        raise subprocess.TimeoutExpired("OpenKBPageIndex.exe", seconds)

    def crash(_input: Path, _output: Path, _seconds: float) -> None:
        raise PageIndexProviderError("pageindex_provider_unavailable", "test crash")

    result = run_pageindex_package_acceptance(
        tmp_path / "acceptance",
        worker,
        valid_invoke=_package_acceptance_result,
        timeout_invoke=timeout,
        crash_invoke=crash,
    )

    assert result["passed"] is True
    assert result["scenarios"] == {
        "timeout": "pageindex_provider_timeout",
        "invalid_tree": "pageindex_provider_invalid_tree",
        "cache_corruption": "rebuilt",
        "provider_crash": "pageindex_provider_unavailable",
        "baseline_available": True,
        "sqlite_integrity": True,
    }
    assert "Ignoring a corrupt official PageIndex provider cache." in caplog.messages


def test_package_evaluation_validation_binds_suite_provider_and_report(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    source_suite = root / "desktop" / "test-assets" / "pageindex-evaluation" / "fixed-suite.json"
    package_root = tmp_path / "package"
    pageindex_root = package_root / "runtime" / "pageindex"
    pageindex_root.mkdir(parents=True)
    suite_path = pageindex_root / "fixed-suite.json"
    suite_path.write_bytes(source_suite.read_bytes())
    corpus_digest, corpus_files = pageindex_evaluation_corpus_identity(source_suite)
    for file_name in corpus_files:
        (pageindex_root / file_name).write_bytes((source_suite.parent / file_name).read_bytes())
    worker = pageindex_root / "OpenKBPageIndex.exe"
    worker.write_bytes(b"fixed packaged worker")
    worker_sha256 = hashlib.sha256(worker.read_bytes()).hexdigest()
    source_report = root / "desktop" / "acceptance" / "2026-08-20-pageindex-retrieval-report.json"
    report_payload = json.loads(source_report.read_text(encoding="utf-8"))
    report_payload["pageindex_worker_sha256"] = worker_sha256
    report_path = tmp_path / "bound-report.json"
    report_path.write_text(json.dumps(report_payload), encoding="utf-8")
    suite = DesktopRetrievalEvaluationSuite.from_json(suite_path)
    manifest_path = package_root / "release-manifest.json"
    manifest = {
        "schemaVersion": 3,
        "experimentalProviders": {
            "pageIndex": {
                "defaultEnabled": False,
                "providerKind": PAGEINDEX_PROVIDER_KIND,
                "providerVersion": PAGEINDEX_PROVIDER_VERSION,
                "entryPoint": "runtime/pageindex/OpenKBPageIndex.exe",
                "evaluation": {
                    "suiteSnapshotId": suite.snapshot_id,
                    "suiteDigest": suite.digest,
                    "caseCount": len(suite.cases),
                    "corpusDigest": corpus_digest,
                    "corpusFiles": list(corpus_files),
                    "variants": [
                        "fts",
                        "structure_lexical",
                        "wiki",
                        "baseline",
                        "local_graph",
                        "document_page_tree",
                        "catalog + document_page_tree",
                    ],
                },
            }
        },
        "files": [
            {
                "path": "runtime/pageindex/OpenKBPageIndex.exe",
                "sha256": worker_sha256,
                "bytes": worker.stat().st_size,
            }
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    validation = validate_pageindex_evaluation(manifest_path, suite_path, report_path)

    assert validation["valid"] is True
    assert validation["passed"] is False
    assert validation["suite_digest"] == suite.digest
    assert validation["provider_version"] == PAGEINDEX_PROVIDER_VERSION
    assert validation["report_sha256"] == hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert validation["corpus_digest"] == corpus_digest
    assert validation["worker_sha256"] == worker_sha256

    worker.write_bytes(b"changed packaged worker")
    changed_worker_sha256 = hashlib.sha256(worker.read_bytes()).hexdigest()
    manifest["files"][0]["sha256"] = changed_worker_sha256
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus or worker"):
        validate_pageindex_evaluation(manifest_path, suite_path, report_path)
    worker.write_bytes(b"fixed packaged worker")
    manifest["files"][0]["sha256"] = worker_sha256

    corpus_file = pageindex_root / corpus_files[0]
    original_corpus = corpus_file.read_bytes()
    corpus_file.write_bytes(original_corpus + b"\nchanged")
    changed_corpus_digest, _files = pageindex_evaluation_corpus_identity(suite_path)
    manifest["experimentalProviders"]["pageIndex"]["evaluation"]["corpusDigest"] = (
        changed_corpus_digest
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus or worker"):
        validate_pageindex_evaluation(manifest_path, suite_path, report_path)
    corpus_file.write_bytes(original_corpus)
    manifest["experimentalProviders"]["pageIndex"]["evaluation"]["corpusDigest"] = corpus_digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    invalid_report = tmp_path / "unbound-report.json"
    invalid_report.write_text('{"gate":{"passed":true}}', encoding="utf-8")
    with pytest.raises(ValueError):
        validate_pageindex_evaluation(manifest_path, suite_path, invalid_report)

    incomplete_payload = json.loads(report_path.read_text(encoding="utf-8"))
    incomplete_payload["results"].pop()
    incomplete_report = tmp_path / "incomplete-report.json"
    incomplete_report.write_text(json.dumps(incomplete_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="coverage"):
        validate_pageindex_evaluation(manifest_path, suite_path, incomplete_report)

    forged_payload = json.loads(report_path.read_text(encoding="utf-8"))
    forged_payload["gate"] = {key: True for key in forged_payload["gate"]}
    forged_report = tmp_path / "forged-gate-report.json"
    forged_report.write_text(json.dumps(forged_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="gate"):
        validate_pageindex_evaluation(manifest_path, suite_path, forged_report)

    unbound_payload = json.loads(report_path.read_text(encoding="utf-8"))
    unbound_payload["page_tree_generations"].pop()
    unbound_report = tmp_path / "unbound-generation-report.json"
    unbound_report.write_text(json.dumps(unbound_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="gate"):
        validate_pageindex_evaluation(manifest_path, suite_path, unbound_report)

    catalogless_payload = json.loads(report_path.read_text(encoding="utf-8"))
    catalogless_payload["catalog_generation_ids"] = []
    for result in catalogless_payload["results"]:
        result["catalog_generation_ids"] = []
    catalogless_report = tmp_path / "catalogless-report.json"
    catalogless_report.write_text(json.dumps(catalogless_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="gate"):
        validate_pageindex_evaluation(manifest_path, suite_path, catalogless_report)

    stale_snapshot_payload = json.loads(report_path.read_text(encoding="utf-8"))
    stale_snapshot_payload["knowledge_snapshot_digest"] = "0" * 64
    stale_snapshot_report = tmp_path / "stale-snapshot-report.json"
    stale_snapshot_report.write_text(json.dumps(stale_snapshot_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="gate"):
        validate_pageindex_evaluation(manifest_path, suite_path, stale_snapshot_report)

    manifest["experimentalProviders"]["pageIndex"]["providerVersion"] = "stale-provider"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_pageindex_evaluation(manifest_path, suite_path, report_path)


def test_experimental_dependency_is_exact_isolated_and_vectorless() -> None:
    root = Path(__file__).resolve().parents[1]
    lock = (root / "requirements-pageindex-experimental.lock").read_text(encoding="utf-8")
    build_lock = (root / "requirements-pageindex-build.lock").read_text(encoding="utf-8")
    package_script = (root / "desktop" / "scripts" / "New-PortablePackage.ps1").read_text(
        encoding="utf-8"
    )
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "pageindex-0.2.10-py3-none-any.whl#sha256=" in lock
    assert PAGEINDEX_PROVIDER_VERSION == "0.2.10+ba0ef02d7803.openkb1"
    assert "ba0ef02d78034704be049894c463dc606acbd0d7" in (
        root / "desktop" / "THIRD_PARTY_NOTICES.md"
    ).read_text(encoding="utf-8")
    assert '"pageindex==' not in project.casefold()
    assert '"pageindex @' not in project.casefold()
    assert all(name not in lock.casefold() for name in ("faiss", "embedding", "chromadb"))
    assert "pyinstaller==6.22.0" in build_lock
    assert "--onedir" in package_script
    assert "--copy-metadata pageindex" in package_script
    assert "uv run --directory $repoRoot python -c" in package_script
    assert "defaultEnabled = $false" in package_script
    assert (root / "desktop" / "licenses" / "PageIndex-MIT.txt").is_file()
