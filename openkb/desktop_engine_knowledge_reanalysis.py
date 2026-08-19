"""Desktop Engine dispatch and background coordination for Knowledge Reanalysis."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.desktop_knowledge_reanalysis import DesktopKnowledgeReanalysisService

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
    from openkb.desktop_model_gateway import DesktopModelGateway

logger = logging.getLogger(__name__)


def dispatch_knowledge_reanalysis_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Read work or start one explicit, non-blocking Reanalysis run."""
    from openkb.desktop_engine import DesktopRequestError, _required_string_param

    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None:
            if request.method == "workbench.knowledge_reanalysis":
                return {"documents": [], "runs": []}
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before starting Knowledge Reanalysis.",
            )
        kb_dir = Path(active.kb_dir)
        service = DesktopKnowledgeReanalysisService(kb_dir)
        if request.method == "workbench.knowledge_reanalysis":
            return service.overview()

        server._begin_workspace_mutation(request, cancel_event)
        gateway = server._model_gateway_factory(kb_dir, None)
        if gateway is None:
            raise DesktopRequestError(
                "model_configuration_invalid",
                "Configure a model provider before starting Knowledge Reanalysis.",
            )
        if request.method == "workbench.start_knowledge_reanalysis":
            run = service.create_run(
                _required_document_ids(request),
                provider=gateway.provider_name,
                model=gateway.model_name,
            )
        else:
            run = service.retry_job(
                _required_string_param(request, "job_id"),
                provider=gateway.provider_name,
                model=gateway.model_name,
            )
        _start_worker(server, kb_dir, run.run_id, gateway)
        return run.as_dict()


def _required_document_ids(request: DesktopRequest) -> tuple[str, ...]:
    from openkb.desktop_engine import DesktopRequestError

    values = request.params.get("document_ids")
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value for value in values)
    ):
        raise DesktopRequestError(
            "invalid_params",
            "workbench.start_knowledge_reanalysis requires document_ids.",
        )
    return tuple(values)


def _start_worker(
    server: DesktopEngineServer,
    kb_dir: Path,
    run_id: str,
    gateway: DesktopModelGateway,
) -> None:
    with server._workers_lock:
        lease = server._knowledge_reanalysis_lease
        worker = threading.Thread(
            target=_run_jobs,
            args=(server, kb_dir, run_id, gateway, lease),
            daemon=True,
            name=f"openkb-engine-knowledge-reanalysis-{run_id[:8]}",
        )
        server._workers.add(worker)
    worker.start()


def _run_jobs(
    server: DesktopEngineServer,
    kb_dir: Path,
    run_id: str,
    gateway: DesktopModelGateway,
    lease: int,
) -> None:
    service = DesktopKnowledgeReanalysisService(kb_dir)

    def should_stop() -> bool:
        return server._shutdown.is_set() or server._knowledge_reanalysis_lease != lease

    try:
        for job_id in service.pending_job_ids(run_id):
            if should_stop():
                break
            service.run_job(job_id, gateway, should_stop=should_stop)
            server._emit_event("knowledge_reanalysis.updated", {"run_id": run_id, "job_id": job_id})
    except Exception:
        logger.exception("Knowledge Reanalysis worker failed for run %s", run_id)
    finally:
        with server._workers_lock:
            server._workers.discard(threading.current_thread())


def invalidate_knowledge_reanalysis_workers(server: DesktopEngineServer) -> None:
    """Invalidate in-process Reanalysis workers before the active KB changes."""
    with server._workers_lock:
        server._knowledge_reanalysis_lease += 1
