"""Engine-owned coordination for optional PageTree enrichment workers."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from openkb.config import load_config_mapping
from openkb.desktop_page_tree_enrichment import DesktopPageTreeEnrichmentService

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
    from openkb.desktop_model_gateway import DesktopModelGateway

logger = logging.getLogger(__name__)


def _page_tree_enrichment_enabled(kb_dir: Path) -> bool:
    """Treat malformed or unavailable optional configuration as disabled."""
    try:
        config = load_config_mapping(kb_dir / ".openkb" / "config.yaml")
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return False
    desktop = config.get("desktop")
    if not isinstance(desktop, dict):
        return True
    value = desktop.get("page_tree_enrichment_enabled")
    return value if isinstance(value, bool) else True


def start_page_tree_enrichments(
    server: DesktopEngineServer,
    kb_dir: Path,
    gateway: DesktopModelGateway | None,
    *,
    recover: bool = False,
    retry_failed: bool = False,
) -> None:
    """Queue eligible trees and run one low-priority worker for this Engine/KB."""
    resolved = kb_dir.expanduser().resolve()
    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None or Path(active.kb_dir).expanduser().resolve() != resolved:
            return
        service = DesktopPageTreeEnrichmentService(resolved)
        if recover:
            try:
                service.recover_interrupted()
            except (OSError, sqlite3.Error):
                logger.warning("Could not recover interrupted PageTree enrichment work.")
        if gateway is None or not _page_tree_enrichment_enabled(resolved):
            return
        with server._workers_lock:
            server._page_tree_enrichment_gateways[resolved] = gateway
            if retry_failed:
                server._page_tree_enrichment_retries.add(resolved)
                server._page_tree_enrichment_cancelled = {
                    key for key in server._page_tree_enrichment_cancelled if key[0] != resolved
                }
            if resolved in server._page_tree_enrichment_workers:
                server._page_tree_enrichment_reruns.add(resolved)
                return
            server._page_tree_enrichment_workers.add(resolved)
            lease = server._page_tree_enrichment_lease
            worker = threading.Thread(
                target=_run_worker,
                args=(server, resolved, lease),
                daemon=True,
                name=f"openkb-page-tree-enrichment-{resolved.name[:24]}",
            )
            server._workers.add(worker)
    try:
        worker.start()
    except RuntimeError:
        with server._workers_lock:
            server._workers.discard(worker)
            server._page_tree_enrichment_workers.discard(resolved)
            server._page_tree_enrichment_reruns.discard(resolved)
            server._page_tree_enrichment_retries.discard(resolved)
            server._page_tree_enrichment_gateways.pop(resolved, None)
        logger.exception("Could not start PageTree enrichment worker for %s", resolved)


def _run_worker(server: DesktopEngineServer, kb_dir: Path, lease: int) -> None:
    released = False

    def should_stop() -> bool:
        return server._shutdown.is_set() or server._page_tree_enrichment_lease != lease

    def document_cancelled(document_id: str) -> bool:
        with server._workers_lock:
            return (kb_dir, document_id) in server._page_tree_enrichment_cancelled

    try:
        service = DesktopPageTreeEnrichmentService(kb_dir)
        while not should_stop():
            with server._workers_lock:
                if server._shutdown.is_set() or server._page_tree_enrichment_lease != lease:
                    break
                gateway = server._page_tree_enrichment_gateways.get(kb_dir)
                server._page_tree_enrichment_reruns.discard(kb_dir)
            if gateway is None:
                break
            if service.deterministic_work_active():
                server._shutdown.wait(0.2)
                continue
            with server._workers_lock:
                retry_failed = kb_dir in server._page_tree_enrichment_retries
                server._page_tree_enrichment_retries.discard(kb_dir)
            service.queue_eligible(gateway, retry_failed=retry_failed)
            for document_id in service.pending_document_ids(gateway):
                if should_stop() or service.deterministic_work_active():
                    break
                if document_cancelled(document_id):
                    continue

                def should_stop_document() -> bool:
                    return should_stop() or document_cancelled(document_id)

                service.run_document(
                    document_id,
                    gateway,
                    should_stop=should_stop_document,
                )
            if should_stop():
                break
            if service.deterministic_work_active():
                server._shutdown.wait(0.2)
                continue
            service.queue_eligible(gateway)
            if any(
                not document_cancelled(document_id)
                for document_id in service.pending_document_ids(gateway)
            ):
                continue
            with server._workers_lock:
                if kb_dir in server._page_tree_enrichment_reruns:
                    continue
                server._page_tree_enrichment_workers.discard(kb_dir)
                server._page_tree_enrichment_gateways.pop(kb_dir, None)
                released = True
            return
    except Exception:
        logger.exception("PageTree enrichment worker failed for %s", kb_dir)
    finally:
        restart_gateway = None
        if not released:
            with server._workers_lock:
                rerun = kb_dir in server._page_tree_enrichment_reruns
                server._page_tree_enrichment_reruns.discard(kb_dir)
                if not rerun:
                    server._page_tree_enrichment_retries.discard(kb_dir)
                server._page_tree_enrichment_workers.discard(kb_dir)
                restart_gateway = server._page_tree_enrichment_gateways.pop(kb_dir, None)
            active = server._workspace.active()
            if (
                rerun
                and restart_gateway is not None
                and active is not None
                and Path(active.kb_dir).expanduser().resolve() == kb_dir
                and not server._shutdown.is_set()
            ):
                start_page_tree_enrichments(server, kb_dir, restart_gateway)
        with server._workers_lock:
            server._workers.discard(threading.current_thread())


def dispatch_page_tree_enrichment_control(
    server: DesktopEngineServer,
    request: DesktopRequest,
) -> dict[str, object]:
    """Cancel or explicitly resume one optional PageTree model task."""
    from openkb.desktop_engine import DesktopRequestError, _required_string_param

    document_id = _required_string_param(request, "document_id")
    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None:
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before controlling PageTree enrichment.",
            )
        kb_dir = Path(active.kb_dir).expanduser().resolve()
        service = DesktopPageTreeEnrichmentService(kb_dir)
        if request.method == "workbench.cancel_page_tree_enrichment":
            accepted = service.request_cancel(document_id)
            if accepted:
                with server._workers_lock:
                    server._page_tree_enrichment_cancelled.add((kb_dir, document_id))
            return {"document_id": document_id, "accepted": accepted}
        gateway = server._model_gateway_factory(kb_dir, None)
        if gateway is None:
            return {"document_id": document_id, "accepted": False}
        accepted = service.retry_document(document_id, gateway)
        if accepted:
            with server._workers_lock:
                server._page_tree_enrichment_cancelled.discard((kb_dir, document_id))
            start_page_tree_enrichments(server, kb_dir, gateway)
        return {"document_id": document_id, "accepted": accepted}


def invalidate_page_tree_enrichment_workers(server: DesktopEngineServer) -> None:
    """Invalidate workers before changing the active Knowledge Base."""
    with server._workers_lock:
        server._page_tree_enrichment_lease += 1
