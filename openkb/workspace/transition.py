"""Coordinate Import Job leases with Active Knowledge Base transitions."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from openkb.importing.runner import DesktopImportControl

if TYPE_CHECKING:
    from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


@dataclass(frozen=True)
class DesktopImportLease:
    """One Import Job admitted against a stable Active Knowledge Base binding."""

    kb_dir: Path
    control: DesktopImportControl
    lease_id: int


class DesktopWorkspaceTransitionCoordinator:
    """Pause admitted imports before rebinding one Desktop Runtime."""

    def __init__(self, workspace: DesktopKnowledgeBaseRuntime) -> None:
        self._workspace = workspace
        self._condition = threading.Condition(threading.Lock())
        self._transitioning = False
        self._next_lease_id = 0
        self._leases: dict[int, DesktopImportLease] = {}

    @contextmanager
    def import_job(
        self, *, expected_kb_dir: Path | None = None
    ) -> Iterator[DesktopImportLease | None]:
        """Admit one import after any in-flight KB transition completes."""
        with self._condition:
            while self._transitioning:
                self._condition.wait()
            active = self._workspace.active()
            if active is None:
                lease = None
            else:
                kb_dir = Path(active.kb_dir).expanduser().resolve()
                expected = (
                    expected_kb_dir.expanduser().resolve() if expected_kb_dir is not None else None
                )
                if expected is not None and expected != kb_dir:
                    lease = None
                else:
                    self._next_lease_id += 1
                    lease = DesktopImportLease(
                        kb_dir,
                        DesktopImportControl(),
                        self._next_lease_id,
                    )
                    self._leases[lease.lease_id] = lease
        try:
            yield lease
        finally:
            if lease is not None:
                with self._condition:
                    self._leases.pop(lease.lease_id, None)
                    self._condition.notify_all()

    @contextmanager
    def activation(self, target_kb_dir: Path) -> Iterator[None]:
        """Close admission and drain old-KB imports before one binding transaction."""
        target = target_kb_dir.expanduser().resolve()
        with self._condition:
            while self._transitioning:
                self._condition.wait()
            self._transitioning = True
            try:
                active = self._workspace.active()
                current = Path(active.kb_dir).expanduser().resolve() if active is not None else None
                if current is not None and current != target:
                    for lease in self._leases.values():
                        if lease.kb_dir == current:
                            lease.control.request_pause()
                    while any(lease.kb_dir == current for lease in self._leases.values()):
                        self._condition.wait()
            except BaseException:
                self._transitioning = False
                self._condition.notify_all()
                raise
        try:
            yield
        finally:
            with self._condition:
                self._transitioning = False
                self._condition.notify_all()
