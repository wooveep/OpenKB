"""Experimental official PageIndex adapter and fixed-evaluation boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_import_artifacts import DocumentIRBlock, build_evidence
from openkb.desktop_page_tree import DETERMINISTIC_PROVIDER_KIND
from openkb.desktop_pageindex_adapter import (
    PAGEINDEX_PROVIDER_KIND,
    PAGEINDEX_PROVIDER_VERSION,
    PageIndexProviderError,
    _subprocess_invoker,
    build_official_pageindex_generation,
)
from openkb.desktop_pageindex_provider import materialize_official_pageindex_provider
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_evaluation import DesktopRetrievalEvaluator
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


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


def _refresh_cache_digest(payload) -> None:
    checkpoint_text = json.dumps(
        payload["checkpoint"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    payload["checkpoint_sha256"] = hashlib.sha256(checkpoint_text.encode("utf-8")).hexdigest()


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


def test_adapter_normalizes_pageindex_nodes_to_existing_evidence_and_safe_cache(tmp_path) -> None:
    blocks = _blocks()
    evidence = build_evidence(blocks)
    cache_dir = tmp_path / "cache"
    calls = 0

    def invoke(input_path: Path, output_path: Path, timeout: float) -> None:
        nonlocal calls
        calls += 1
        _pageindex_result(input_path, output_path, timeout)

    generation = build_official_pageindex_generation(
        "document-1", blocks, evidence, (), cache_dir=cache_dir, invoke=invoke
    )
    assert generation.provider_kind == PAGEINDEX_PROVIDER_KIND
    assert generation.provider_version == PAGEINDEX_PROVIDER_VERSION
    assert [node.title for node in generation.nodes] == ["Document", "Guide", "Detail"]
    assert {binding.evidence_id for node in generation.nodes for binding in node.evidence} == {
        evidence_id for evidence_id, _block in evidence
    }
    assert calls == 1

    cached = build_official_pageindex_generation(
        "document-1",
        blocks,
        evidence,
        (),
        cache_dir=cache_dir,
        invoke=lambda *_args: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert cached == generation
    cache_text = next(cache_dir.glob("*.json")).read_text(encoding="utf-8")
    assert "Alpha fact." not in cache_text and "Beta fact." not in cache_text

    cache_file = next(cache_dir.glob("*.json"))
    tampered = json.loads(cache_file.read_text(encoding="utf-8"))
    tampered["checkpoint"]["generation"]["nodes"][1]["title"] = "Tampered title"
    _refresh_cache_digest(tampered)
    cache_file.write_text(json.dumps(tampered), encoding="utf-8")
    rebuilt = build_official_pageindex_generation(
        "document-1", blocks, evidence, (), cache_dir=cache_dir, invoke=invoke
    )
    assert rebuilt.generation_id == generation.generation_id
    assert calls == 2

    tampered = json.loads(cache_file.read_text(encoding="utf-8"))
    for node in tampered["checkpoint"]["generation"]["nodes"]:
        node["evidence_ids"] = []
        node["evidence_block_ordinals"] = []
    _refresh_cache_digest(tampered)
    cache_file.write_text(json.dumps(tampered), encoding="utf-8")
    build_official_pageindex_generation(
        "document-1", blocks, evidence, (), cache_dir=cache_dir, invoke=invoke
    )
    assert calls == 3

    cache_file.write_text("not-json", encoding="utf-8")
    build_official_pageindex_generation(
        "document-1", blocks, evidence, (), cache_dir=cache_dir, invoke=invoke
    )
    assert calls == 4


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
            cache_dir=tmp_path / "cache",
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
            cache_dir=tmp_path / "cache",
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
            cache_dir=tmp_path / "cache",
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


def test_experimental_dependency_is_exact_isolated_and_vectorless() -> None:
    root = Path(__file__).resolve().parents[1]
    lock = (root / "requirements-pageindex-experimental.lock").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "pageindex-0.2.10-py3-none-any.whl#sha256=" in lock
    assert PAGEINDEX_PROVIDER_VERSION == "0.2.10+ba0ef02d7803.openkb1"
    assert "ba0ef02d78034704be049894c463dc606acbd0d7" in (
        root / "desktop" / "THIRD_PARTY_NOTICES.md"
    ).read_text(encoding="utf-8")
    assert '"pageindex==' not in project.casefold()
    assert '"pageindex @' not in project.casefold()
    assert all(name not in lock.casefold() for name in ("faiss", "embedding", "chromadb"))
