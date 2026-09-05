"""Desktop Engine dispatch and background coordination for Knowledge Reanalysis."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb import desktop_engine_knowledge_graph as knowledge_graph_engine
from openkb.desktop_knowledge_reanalysis import DesktopKnowledgeReanalysisService
from openkb.desktop_model_result_failure import (
    authorize_model_operation_retry,
    revoke_model_operation_retry_scope,
)

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
    authorized_contracts: set[tuple[str, str, str]] = set()
    authorization_lock = threading.Lock()

    def authorize_retry(request) -> str:
        key = (
            request.operation,
            request.capability_identity or "",
            request.prompt_contract_digest or "",
        )
        with authorization_lock:
            if key not in authorized_contracts:
                authorize_model_operation_retry(
                    kb_dir,
                    gateway,
                    operation=request.operation,
                    retry_scope=run_id,
                    capability_identity=request.capability_identity,
                    prompt_contract_digest=request.prompt_contract_digest,
                )
                authorized_contracts.add(key)
        return run_id

    def should_stop() -> bool:
        return server._shutdown.is_set() or server._knowledge_reanalysis_lease != lease

    try:
        authorize_model_operation_retry(
            kb_dir,
            gateway,
            operation="entity_dossier_planning",
            retry_scope=run_id,
        )
        for job_id in service.pending_job_ids(run_id):
            if should_stop():
                break
            graph_document_ids = service.run_job(
                job_id,
                gateway,
                should_stop=should_stop,
                authorize_retry=authorize_retry,
                retry_scope=run_id,
            )
            if graph_document_ids:
                with server._workers_lock:
                    for document_id in graph_document_ids:
                        server._knowledge_graph_extraction_cancelled.discard((kb_dir, document_id))
                knowledge_graph_engine.start_knowledge_graph_extractions(server, kb_dir, gateway)
            server._emit_event("knowledge_reanalysis.updated", {"run_id": run_id, "job_id": job_id})
    except Exception:
        logger.exception("Knowledge Reanalysis worker failed for run %s", run_id)
    finally:
        revoke_model_operation_retry_scope(kb_dir, run_id)
        with server._workers_lock:
            server._workers.discard(threading.current_thread())


def invalidate_knowledge_reanalysis_workers(server: DesktopEngineServer) -> None:
    """Invalidate in-process Reanalysis workers before the active KB changes."""
    with server._workers_lock:
        server._knowledge_reanalysis_lease += 1
