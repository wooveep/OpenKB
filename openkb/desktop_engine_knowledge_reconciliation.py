"""Read-only Desktop Engine requests for the knowledge reconciliation queue."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_knowledge_reconciliation_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Expose pending conflicts without allowing transport code to infer state."""
    from openkb.desktop_engine import DesktopRequestError

    with server._workspace_requests_lock:
        if cancel_event is not None and cancel_event.is_set():
            raise DesktopRequestError("request_cancelled", "Desktop Bridge request was cancelled.")
        active = server._workspace.active()
        if active is None:
            return {"conflicts": []}
        service = DesktopKnowledgeReconciliationService(Path(active.kb_dir))
        return {"conflicts": [conflict.as_dict() for conflict in service.list_conflicts()]}
