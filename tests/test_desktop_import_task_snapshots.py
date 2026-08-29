"""Concurrency contract for Desktop task-center snapshots."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_import_task_snapshots import DesktopImportTaskSnapshots
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_dir
from openkb.locks import kb_ingest_lock


def test_concurrent_snapshot_reads_share_one_projection(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def load(kb_dir: Path) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return {"jobs": [str(kb_dir)]}

    snapshots = DesktopImportTaskSnapshots(loader=load)
    results: list[dict[str, object]] = []
    callers_ready = threading.Barrier(21)

    def read_snapshot() -> None:
        callers_ready.wait()
        results.append(snapshots.read(tmp_path))

    workers = [threading.Thread(target=read_snapshot) for _ in range(20)]

    for worker in workers:
        worker.start()
    callers_ready.wait()
    assert started.wait(timeout=1)
    assert not release.wait(timeout=0.05)
    with calls_lock:
        assert calls == 1
    release.set()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 20
    assert all(result == {"jobs": [str(tmp_path.resolve())]} for result in results)
    with calls_lock:
        assert calls == 1


def test_sequential_snapshot_reads_are_never_cached(tmp_path: Path) -> None:
    calls = 0

    def load(_kb_dir: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"jobs": [calls]}

    snapshots = DesktopImportTaskSnapshots(loader=load)

    assert snapshots.read(tmp_path) == {"jobs": [1]}
    assert snapshots.read(tmp_path) == {"jobs": [2]}


def test_failed_snapshot_flight_does_not_block_the_next_read(tmp_path: Path) -> None:
    calls = 0

    def load(_kb_dir: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("projection unavailable")
        return {"jobs": []}

    snapshots = DesktopImportTaskSnapshots(loader=load)

    with pytest.raises(RuntimeError, match="projection unavailable"):
        snapshots.read(tmp_path)
    assert snapshots.read(tmp_path) == {"jobs": []}


def test_task_projection_waits_for_the_kb_mutation_lock(tmp_path: Path) -> None:
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    reader_started = threading.Event()
    reader_finished = threading.Event()
    results: list[dict[str, object]] = []

    def read_tasks() -> None:
        reader_started.set()
        results.append(DesktopTextImportService(kb_dir).list_import_jobs())
        reader_finished.set()

    with kb_ingest_lock(desktop_state_dir(kb_dir)):
        worker = threading.Thread(target=read_tasks)
        worker.start()
        assert reader_started.wait(timeout=1)
        assert not reader_finished.wait(timeout=0.05)

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0]["jobs"] == []
