"""Engine-owned page synthesis, independent of optional graph extraction."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from openkb.config import preferred_knowledge_language
from openkb.knowledge.corpus.knowledge_pipeline import CorpusKnowledgeSynthesisPipeline
from openkb.knowledge.corpus.work_queue import CorpusWorkQueue
from openkb.models.gateway import DesktopModelGateway, gateway_analysis_capability_verified

if TYPE_CHECKING:
    from openkb.engine.server import DesktopEngineServer

logger = logging.getLogger(__name__)


class CorpusSynthesisWorkers:
    """Coalesce arrivals under one worker per KB and the Engine's transition lease."""

    def __init__(self, server: DesktopEngineServer):
        self._server = server
        self._lease = 0
        self._gateways: dict[Path, DesktopModelGateway] = {}
        self._running: set[Path] = set()
        self._reruns: set[Path] = set()

    def start(self, kb_dir: Path, gateway: DesktopModelGateway | None) -> None:
        if gateway is None or not gateway_analysis_capability_verified(gateway):
            return
        kb_dir = kb_dir.expanduser().resolve()
        server = self._server
        with server._workspace_requests_lock:
            active = server._workspace.active()
            if active is None or Path(active.kb_dir).resolve() != kb_dir:
                return
            with server._workers_lock:
                self._gateways[kb_dir] = gateway
                if kb_dir in self._running:
                    self._reruns.add(kb_dir)
                    return
                self._running.add(kb_dir)
                worker = threading.Thread(
                    target=self._run,
                    args=(kb_dir, self._lease),
                    daemon=True,
                    name="openkb-corpus-synthesis",
                )
                server._workers.add(worker)
        try:
            worker.start()
        except RuntimeError:
            with server._workers_lock:
                self._running.discard(kb_dir)
                self._gateways.pop(kb_dir, None)
                server._workers.discard(worker)
            logger.exception("Could not start corpus synthesis worker")

    def invalidate(self) -> None:
        with self._server._workers_lock:
            self._lease += 1
            self._gateways.clear()
            self._reruns.clear()

    def retire(self, kb_dir: Path) -> None:
        with self._server._workers_lock:
            self._gateways.pop(kb_dir.expanduser().resolve(), None)
            self._reruns.discard(kb_dir.expanduser().resolve())

    def _run(self, kb_dir: Path, lease: int) -> None:
        server = self._server
        queue = CorpusWorkQueue(kb_dir)
        revisions: dict[str, int] = {}

        def should_stop() -> bool:
            return server._shutdown.is_set() or lease != self._lease

        try:
            while not should_stop():
                with server._workers_lock:
                    gateway = self._gateways.get(kb_dir)
                    self._reruns.discard(kb_dir)
                if gateway is None:
                    break
                revisions = queue.pending()
                if not revisions:
                    break
                outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
                    affected_document_ids=tuple(revisions),
                    force_generation=True,
                    preferred_language=preferred_knowledge_language(kb_dir),
                    gateway=gateway,
                    should_stop=should_stop,
                    can_dispatch=lambda: self._gateways.get(kb_dir) is gateway,
                )
                if should_stop():
                    break
                if self._gateways.get(kb_dir) is not gateway:
                    continue
                error = None if outcome.status in {"active", "unchanged"} else outcome.status
                queue.finish(revisions, error_code=error)
        except Exception:
            logger.exception("Corpus synthesis failed; explicit retry is required")
            queue.finish(revisions, error_code="corpus_synthesis_failed")
        finally:
            with server._workers_lock:
                rerun = kb_dir in self._reruns
                gateway = self._gateways.pop(kb_dir, None)
                self._reruns.discard(kb_dir)
                self._running.discard(kb_dir)
                server._workers.discard(threading.current_thread())
            if rerun and gateway is not None and not server._shutdown.is_set():
                self.start(kb_dir, gateway)
