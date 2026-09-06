"""Single-flight snapshots for the Desktop task-center projection."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

TaskSnapshot = dict[str, object]
TaskSnapshotLoader = Callable[[Path], TaskSnapshot]


@dataclass
class _SnapshotFlight:
    done: bool = False
    value: TaskSnapshot | None = None
    error: BaseException | None = None


class DesktopImportTaskSnapshots:
    """Collapse overlapping projection reads without caching completed results."""

    def __init__(self, *, loader: TaskSnapshotLoader | None = None) -> None:
        self._loader = loader or _load_task_snapshot
        self._condition = threading.Condition()
        self._flights: dict[Path, _SnapshotFlight] = {}

    def read(self, kb_dir: Path) -> TaskSnapshot:
        """Return a current snapshot, sharing only a genuinely in-flight read."""
        resolved = kb_dir.expanduser().resolve()
        with self._condition:
            flight = self._flights.get(resolved)
            leader = flight is None
            if flight is None:
                flight = _SnapshotFlight()
                self._flights[resolved] = flight

        if leader:
            try:
                value = self._loader(resolved)
            except BaseException as error:
                with self._condition:
                    flight.error = error
                    flight.done = True
                    self._flights.pop(resolved, None)
                    self._condition.notify_all()
                raise
            with self._condition:
                flight.value = value
                flight.done = True
                self._flights.pop(resolved, None)
                self._condition.notify_all()
            return value

        with self._condition:
            while not flight.done:
                self._condition.wait()
            if flight.error is not None:
                raise flight.error
            assert flight.value is not None
            return flight.value


def _load_task_snapshot(kb_dir: Path) -> TaskSnapshot:
    from openkb.importing.service import DesktopTextImportService

    return DesktopTextImportService(kb_dir).list_import_jobs()
