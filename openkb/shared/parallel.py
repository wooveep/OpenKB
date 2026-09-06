"""Small bounded parallel execution helpers for independent Desktop work."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TypeVar, cast

Item = TypeVar("Item")
Result = TypeVar("Result")


def parallel_map_ordered(
    items: Sequence[Item],
    worker: Callable[[Item], Result],
    *,
    maximum: int,
    on_completed: Callable[[], None],
) -> tuple[Result, ...]:
    """Run independent items concurrently while returning their original order."""
    if not items:
        return ()
    workers = max(1, min(maximum, len(items)))
    if workers == 1:
        results: list[Result] = []
        for item in items:
            results.append(worker(item))
            on_completed()
        return tuple(results)

    ordered: list[Result | None] = [None] * len(items)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="openkb-analysis-batch")
    dispatch_lock = threading.Lock()
    failure_seen = False
    remaining = iter(enumerate(items))
    futures: dict[Future[Result], int] = {}

    def guarded_worker(item: Item) -> Result:
        nonlocal failure_seen
        try:
            return worker(item)
        except BaseException:
            with dispatch_lock:
                failure_seen = True
            raise

    def submit_next() -> bool:
        with dispatch_lock:
            if failure_seen:
                return False
            try:
                ordinal, item = next(remaining)
            except StopIteration:
                return False
            futures[executor.submit(guarded_worker, item)] = ordinal
            return True

    for _ in range(workers):
        submit_next()
    try:
        while futures:
            completed, _pending = wait(tuple(futures), return_when=FIRST_COMPLETED)
            first_error: BaseException | None = None
            successful = 0
            for future in completed:
                ordinal = futures.pop(future)
                try:
                    ordered[ordinal] = future.result()
                    on_completed()
                    successful += 1
                except BaseException as error:
                    first_error = first_error or error
            if first_error is not None:
                raise first_error
            for _ in range(successful):
                submit_next()
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    executor.shutdown(wait=True)
    return tuple(cast(Result, result) for result in ordered)
