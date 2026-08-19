"""Deterministic corpus Catalog generation and vectorless routing behavior."""

from __future__ import annotations

import sqlite3

import pytest

from openkb import desktop_catalog_store as catalog_store
from openkb import desktop_retrieval
from openkb.desktop_catalog_retrieval import (
    CATALOG_DIRECT_WEIGHT,
    CATALOG_STALE_MULTIPLIER,
    catalog_route_rows_in,
)
from openkb.desktop_catalog_store import (
    lease_current_catalog,
    queue_catalog_rebuild_in,
    rebuild_pending_catalog,
)
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _controlled_kb(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(catalog_store, "start_catalog_rebuilds", lambda *_args, **_kwargs: None)
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    assert rebuild_pending_catalog(kb_dir)
    return kb_dir


def _source_backed_pages(kb_dir, tmp_path):
    source = tmp_path / "facts.md"
    source.write_text(
        "# Sources\n\nAlpha evidence only.\n\nAlpha evidence only.\n\nBeta evidence only.\n",
        encoding="utf-8",
    )
    DesktopTextImportService(kb_dir).import_text(source)
    assert rebuild_pending_catalog(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    alpha_source = pages.search_sources("Alpha evidence only")[0]
    beta_source = pages.search_sources("Beta evidence only")[0]

    beta = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Configuration",
        content_markdown="Beta routing fact.",
    )
    pages.bind_source(beta.page_id, "Beta routing fact.", beta_source.evidence_id)
    pages.publish(beta.page_id)
    assert rebuild_pending_catalog(kb_dir)

    alpha = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Alpha Router",
        content_markdown=(f"Alpha routing fact.\n\n[Configuration](/concept/{beta.page_id}.md)"),
    )
    pages.bind_source(alpha.page_id, "Alpha routing fact.", alpha_source.evidence_id)
    return pages, alpha, beta, alpha_source.evidence_id, beta_source.evidence_id


def test_catalog_uses_published_snapshot_and_routes_one_low_weight_link(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    pages, alpha, beta, alpha_evidence, beta_evidence = _source_backed_pages(kb_dir, tmp_path)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        before_revision = connection.execute(
            "SELECT source_revision FROM knowledge_catalog_state WHERE singleton = 1"
        ).fetchone()[0]
    pages.save_draft(
        page_id=beta.page_id,
        kind="concept",
        title="Configuration",
        content_markdown="Unpublished replacement.",
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT source_revision FROM knowledge_catalog_state WHERE singleton = 1"
        ).fetchone() == (before_revision,)

    pages.publish(alpha.page_id)
    with sqlite3.connect(database_path) as connection:
        stale = connection.execute(
            "SELECT current_generation_id, is_stale FROM knowledge_catalog_state"
        ).fetchone()
        assert stale[0] is not None and stale[1] == 1
    direct = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha Router")
    assert alpha_evidence in {item.evidence_id for item in direct.evidence}
    assert "catalog_stale" in direct.degradations
    catalog_trace = next(
        channel for channel in direct.retrieval_trace.channels if channel.channel == "catalog"
    )
    assert "catalog_stale" in catalog_trace.degradation_reasons

    assert rebuild_pending_catalog(kb_dir)
    routed = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha Router")
    by_evidence = {item.evidence_id: item for item in routed.evidence}
    assert alpha_evidence in by_evidence
    assert beta_evidence in by_evidence
    assert "catalog" in by_evidence[beta_evidence].channels
    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_catalog_nodes)")
        }
        links = connection.execute(
            """
            SELECT COUNT(*) FROM knowledge_catalog_links AS links
            JOIN knowledge_catalog_state AS state
                ON state.current_generation_id = links.generation_id
            WHERE links.from_node_id = ? AND links.to_node_id = ?
            """,
            (f"page:{alpha.page_id}", f"page:{beta.page_id}"),
        ).fetchone()
    assert "content_markdown" not in columns and "excerpt" not in columns
    assert links == (1,)

    pages.set_stale_after(alpha.page_id, "2020-01-01T00:00:00+00:00")
    assert rebuild_pending_catalog(kb_dir)
    with sqlite3.connect(database_path) as connection:
        generation_id = str(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_catalog_state"
            ).fetchone()[0]
        )
        catalog_rows = catalog_route_rows_in(
            connection, generation_id, ("alpha", "router"), limit=12
        )
    alpha_row = next(row for row in catalog_rows if str(row[0]) == alpha_evidence)
    assert float(alpha_row[6]) == pytest.approx(CATALOG_DIRECT_WEIGHT * CATALOG_STALE_MULTIPLIER)

    pages.deprecate(beta.page_id)
    assert rebuild_pending_catalog(kb_dir)
    without_deprecated_hop = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha Router")
    assert beta_evidence not in {item.evidence_id for item in without_deprecated_hop.evidence}


def test_catalog_failure_serves_previous_generation_and_task_reason(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    pages, alpha, _beta, _alpha_evidence, _beta_evidence = _source_backed_pages(kb_dir, tmp_path)
    pages.publish(alpha.page_id)
    assert rebuild_pending_catalog(kb_dir)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        previous = connection.execute(
            "SELECT current_generation_id FROM knowledge_catalog_state"
        ).fetchone()[0]

    pages.deprecate(alpha.page_id)
    monkeypatch.setattr(
        catalog_store,
        "build_catalog_snapshot_in",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("injected catalog fault")),
    )
    assert not rebuild_pending_catalog(kb_dir)
    task = DesktopTextImportService(kb_dir).list_import_jobs()["catalog_rebuild"]
    assert task["status"] == "failed"
    assert task["stale_serving"] is True
    assert task["error_code"] == "knowledge_catalog_build_failed"
    assert "injected catalog fault" in task["error_reason"]
    with lease_current_catalog(kb_dir) as lease:
        assert lease is not None
        assert lease.generation_id == previous
        assert lease.is_stale


def test_catalog_retries_one_transient_build_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(catalog_store, "start_catalog_rebuilds", lambda *_args, **_kwargs: None)
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    original_build = catalog_store.build_catalog_snapshot_in
    attempts = 0

    def flaky_build(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient catalog fault")
        return original_build(*args, **kwargs)

    monkeypatch.setattr(catalog_store, "build_catalog_snapshot_in", flaky_build)
    assert rebuild_pending_catalog(kb_dir)
    task = DesktopTextImportService(kb_dir).list_import_jobs()["catalog_rebuild"]
    assert attempts == 2
    assert task["status"] == "completed"
    assert task["attempt_count"] == 2


def test_initial_catalog_failure_does_not_claim_a_stale_generation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(catalog_store, "start_catalog_rebuilds", lambda *_args, **_kwargs: None)
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    monkeypatch.setattr(
        catalog_store,
        "build_catalog_snapshot_in",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("persistent catalog fault")),
    )

    assert not rebuild_pending_catalog(kb_dir)
    task = DesktopTextImportService(kb_dir).list_import_jobs()["catalog_rebuild"]
    assert task["status"] == "failed"
    assert task["attempt_count"] == 2
    assert task["current_generation_id"] is None
    assert task["stale_serving"] is False


def test_catalog_faults_drop_only_the_optional_channel(tmp_path, monkeypatch) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    source = tmp_path / "baseline.md"
    source.write_text("# Baseline\n\nAlpha baseline evidence remains available.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(source)
    assert rebuild_pending_catalog(kb_dir)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            desktop_retrieval,
            "catalog_route_rows_in",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("query fault")),
        )
        query_failure = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha baseline evidence")

    class BrokenLease:
        def __enter__(self):
            raise RuntimeError("lease fault")

        def __exit__(self, *_args):
            return False

    with monkeypatch.context() as scoped:
        scoped.setattr(desktop_retrieval, "lease_current_catalog", lambda _kb_dir: BrokenLease())
        lease_failure = DesktopEvidenceRetriever(kb_dir).retrieve("Alpha baseline evidence")

    for pack in (query_failure, lease_failure):
        assert any("Alpha baseline evidence" in item.excerpt for item in pack.evidence)
        assert "catalog_query_failed" in pack.degradations
        catalog_trace = next(
            channel for channel in pack.retrieval_trace.channels if channel.channel == "catalog"
        )
        assert "catalog_query_failed" in catalog_trace.degradation_reasons


def test_catalog_retains_recent_generation_until_an_older_reader_releases(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = _controlled_kb(tmp_path, monkeypatch)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with lease_current_catalog(kb_dir) as first:
        assert first is not None
        for reason in ("test-second", "test-third"):
            with sqlite3.connect(database_path) as connection:
                with connection:
                    queue_catalog_rebuild_in(connection, reason)
            assert rebuild_pending_catalog(kb_dir)
        with sqlite3.connect(database_path) as connection:
            generations = connection.execute(
                "SELECT generation_id FROM knowledge_catalog_generations"
            ).fetchall()
        assert len(generations) == 3
    with sqlite3.connect(database_path) as connection:
        remaining = connection.execute(
            "SELECT status, COUNT(*) FROM knowledge_catalog_generations GROUP BY status"
        ).fetchall()
    assert sorted(remaining) == [("current", 1), ("recent", 1)]


def test_catalog_reader_leases_are_scoped_to_their_knowledge_base(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_kb = _controlled_kb(tmp_path / "first", monkeypatch)
    second_kb = _controlled_kb(tmp_path / "second", monkeypatch)
    second_database = second_kb / ".openkb" / "state.sqlite3"

    with lease_current_catalog(first_kb):
        for reason in ("second-generation", "third-generation"):
            with sqlite3.connect(second_database) as connection:
                with connection:
                    queue_catalog_rebuild_in(connection, reason)
            assert rebuild_pending_catalog(second_kb)

    with sqlite3.connect(second_database) as connection:
        remaining = connection.execute(
            "SELECT status, COUNT(*) FROM knowledge_catalog_generations GROUP BY status"
        ).fetchall()
    assert sorted(remaining) == [("current", 1), ("recent", 1)]
