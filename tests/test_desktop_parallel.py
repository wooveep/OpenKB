"""Bounded parallel helpers stop dispatch without losing active work."""

from __future__ import annotations

import threading

import pytest

from openkb.desktop_parallel import parallel_map_ordered


def test_parallel_map_stops_new_dispatch_after_a_worker_fails() -> None:
    active_started = threading.Event()
    release_active = threading.Event()
    started: list[int] = []
    started_lock = threading.Lock()

    def worker(item: int) -> int:
        with started_lock:
            started.append(item)
        if item == 0:
            active_started.set()
            release_active.wait(timeout=2)
        if item == 1:
            assert active_started.wait(timeout=1)
            release_active.set()
            raise RuntimeError("permanent failure")
        return item

    with pytest.raises(RuntimeError, match="permanent failure"):
        parallel_map_ordered(
            tuple(range(6)),
            worker,
            maximum=2,
            on_completed=lambda: None,
        )

    assert set(started) == {0, 1}


def test_parallel_map_preserves_input_order_with_a_rolling_window() -> None:
    completed = 0

    def record_completion() -> None:
        nonlocal completed
        completed += 1

    results = parallel_map_ordered(
        (3, 2, 1, 0),
        lambda item: item * 2,
        maximum=2,
        on_completed=record_completion,
    )

    assert results == (6, 4, 2, 0)
    assert completed == 4
