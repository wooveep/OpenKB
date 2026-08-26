"""Engine-owned workers and controls for optional Knowledge Graph extraction."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from openkb.desktop_knowledge_graph_tasks import DesktopKnowledgeGraphExtractionTasks
from openkb.desktop_model_gateway import gateway_analysis_capability_verified

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
    from openkb.desktop_model_gateway import DesktopModelGateway

logger = logging.getLogger(__name__)


def start_knowledge_graph_extractions(
    server: DesktopEngineServer,
    kb_dir: Path,
    gateway: DesktopModelGateway | None,
) -> None:
    """Run durably queued graph work under the Engine worker lifecycle."""
    if gateway is None or not gateway_analysis_capability_verified(gateway):
        return
    resolved = kb_dir.expanduser().resolve()
    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None or Path(active.kb_dir).expanduser().resolve() != resolved:
            return
        with server._workers_lock:
            server._knowledge_graph_extraction_gateways[resolved] = gateway
            if resolved in server._knowledge_graph_extraction_workers:
                server._knowledge_graph_extraction_reruns.add(resolved)
                return
            server._knowledge_graph_extraction_workers.add(resolved)
            lease = server._knowledge_graph_extraction_lease
            worker = threading.Thread(
                target=_run_worker,
                args=(server, resolved, lease),
                daemon=True,
                name=f"openkb-knowledge-graph-{resolved.name[:24]}",
            )
            server._workers.add(worker)
    try:
        worker.start()
    except RuntimeError:
        with server._workers_lock:
            server._workers.discard(worker)
            server._knowledge_graph_extraction_workers.discard(resolved)
            server._knowledge_graph_extraction_reruns.discard(resolved)
            server._knowledge_graph_extraction_gateways.pop(resolved, None)
        logger.exception("Could not start Knowledge Graph worker for %s", resolved)


def _run_worker(server: DesktopEngineServer, kb_dir: Path, lease: int) -> None:
    released = False

    def should_stop() -> bool:
        return server._shutdown.is_set() or server._knowledge_graph_extraction_lease != lease

    def document_cancelled(document_id: str) -> bool:
        with server._workers_lock:
            return (kb_dir, document_id) in server._knowledge_graph_extraction_cancelled

    def gateway_is_current(gateway: DesktopModelGateway) -> bool:
        with server._workers_lock:
            current = server._knowledge_graph_extraction_gateways.get(kb_dir) is gateway
        return current and gateway_analysis_capability_verified(gateway)

    try:
        tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
        while not should_stop():
            with server._workers_lock:
                gateway = server._knowledge_graph_extraction_gateways.get(kb_dir)
                server._knowledge_graph_extraction_reruns.discard(kb_dir)
            if gateway is None:
                break
            for document_id in tasks.pending_document_ids(gateway):
                if should_stop() or not gateway_is_current(gateway):
                    break
                if document_cancelled(document_id):
                    continue

                def should_stop_document(document_id: str = document_id) -> bool:
                    return should_stop() or document_cancelled(document_id)

                tasks.run_document(
                    document_id,
                    gateway,
                    should_stop=should_stop_document,
                )
            if should_stop():
                break
            if not gateway_is_current(gateway):
                break
            if any(
                not document_cancelled(document_id)
                for document_id in tasks.pending_document_ids(gateway)
            ):
                continue
            with server._workers_lock:
                if kb_dir in server._knowledge_graph_extraction_reruns:
                    continue
                server._knowledge_graph_extraction_workers.discard(kb_dir)
                server._knowledge_graph_extraction_gateways.pop(kb_dir, None)
                released = True
            return
    except Exception:
        logger.exception("Knowledge Graph worker failed for %s", kb_dir)
    finally:
        restart_gateway = None
        if not released:
            with server._workers_lock:
                rerun = kb_dir in server._knowledge_graph_extraction_reruns
                server._knowledge_graph_extraction_reruns.discard(kb_dir)
                server._knowledge_graph_extraction_workers.discard(kb_dir)
                restart_gateway = server._knowledge_graph_extraction_gateways.pop(kb_dir, None)
            active = server._workspace.active()
            if (
                rerun
                and restart_gateway is not None
                and active is not None
                and Path(active.kb_dir).expanduser().resolve() == kb_dir
                and not server._shutdown.is_set()
            ):
                start_knowledge_graph_extractions(server, kb_dir, restart_gateway)
        with server._workers_lock:
            server._workers.discard(threading.current_thread())


def dispatch_knowledge_graph_control(
    server: DesktopEngineServer,
    request: DesktopRequest,
) -> dict[str, object]:
    """Cancel or explicitly resume one durable graph extraction task."""
    from openkb.desktop_engine import DesktopRequestError, _required_string_param

    document_id = _required_string_param(request, "document_id")
    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None:
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before controlling graph extraction.",
            )
        kb_dir = Path(active.kb_dir).expanduser().resolve()
        tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
        if request.method == "workbench.cancel_knowledge_graph_extraction":
            accepted = tasks.request_cancel(document_id)
            if accepted:
                with server._workers_lock:
                    server._knowledge_graph_extraction_cancelled.add((kb_dir, document_id))
            return {"document_id": document_id, "accepted": accepted}
        gateway = server._model_gateway_factory(kb_dir, None)
        if gateway is None:
            return {"document_id": document_id, "accepted": False}
        accepted = tasks.retry(document_id, gateway)
        if accepted:
            with server._workers_lock:
                server._knowledge_graph_extraction_cancelled.discard((kb_dir, document_id))
            start_knowledge_graph_extractions(server, kb_dir, gateway)
        return {"document_id": document_id, "accepted": accepted}


def invalidate_knowledge_graph_workers(server: DesktopEngineServer) -> None:
    """Invalidate graph workers before a KB or model-settings transition."""
    with server._workers_lock:
        server._knowledge_graph_extraction_lease += 1


def retire_knowledge_graph_gateway(server: DesktopEngineServer, kb_dir: Path) -> None:
    """Let an active attempt finish, but prevent its captured gateway dispatching again."""
    resolved = kb_dir.expanduser().resolve()
    with server._workers_lock:
        server._knowledge_graph_extraction_gateways.pop(resolved, None)
        server._knowledge_graph_extraction_reruns.discard(resolved)
