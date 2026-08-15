"""Desktop Engine handlers for user-confirmed D3 Document Version Candidates."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.desktop_document_versions import DesktopDocumentVersionService

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_document_version_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Expose candidate review without allowing transport code to infer identity."""
    from openkb.desktop_engine import DesktopRequestError, _required_string_param

    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None:
            if request.method == "workbench.document_version_candidates":
                return {"candidates": []}
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before reviewing document versions.",
            )
        service = DesktopDocumentVersionService(Path(active.kb_dir))
        if request.method == "workbench.document_version_candidates":
            return {"candidates": [candidate.as_dict() for candidate in service.list_candidates()]}
        decision = _required_string_param(request, "decision")
        if decision not in {"link_to_candidate", "keep_separate"}:
            raise DesktopRequestError(
                "invalid_params",
                "Document version decision must be link_to_candidate or keep_separate.",
            )
        server._begin_workspace_mutation(request, cancel_event)
        return service.resolve_candidate(
            _required_string_param(request, "candidate_id"), decision
        ).as_dict()
