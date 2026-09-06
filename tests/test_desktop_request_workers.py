"""Concurrency contract for bounded Desktop request execution."""

from __future__ import annotations

import threading

import pytest

from openkb.engine.request_workers import DesktopRequestWorkers


def test_request_workers_bound_active_threads_and_drain_queued_work() -> None:
    workers = DesktopRequestWorkers(maximum=3)
    condition = threading.Condition()
    release = False
    active = 0
    peak = 0
    completed = 0

    def work() -> None:
        nonlocal active, completed, peak
        with condition:
            active += 1
            peak = max(peak, active)
            condition.notify_all()
            condition.wait_for(lambda: release, timeout=2)
            active -= 1
            completed += 1

    try:
        for _ in range(12):
            workers.submit(work)
        with condition:
            assert condition.wait_for(lambda: peak == 3, timeout=1)
            assert active == 3
            release = True
            condition.notify_all()
    finally:
        with condition:
            release = True
            condition.notify_all()
        workers.close()

    assert peak == 3
    assert completed == 12


def test_closed_request_workers_reject_new_work() -> None:
    workers = DesktopRequestWorkers(maximum=1)
    workers.close()

    with pytest.raises(RuntimeError, match="closed"):
        workers.submit(lambda: None)
