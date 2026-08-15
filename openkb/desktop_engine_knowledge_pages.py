"""Knowledge-page requests separated from the Desktop Engine transport loop."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.desktop_knowledge_pages import DesktopKnowledgePageService

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_knowledge_page_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Read or revise the active knowledge base's user-owned pages."""
    from openkb.desktop_engine import DesktopRequestError, _required_string_param

    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None:
            if request.method == "workbench.knowledge_pages":
                return {"pages": []}
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before working with knowledge pages.",
            )
        service = DesktopKnowledgePageService(Path(active.kb_dir))
        if request.method == "workbench.knowledge_pages":
            return {"pages": [page.as_dict() for page in service.list_pages()]}
        if request.method == "workbench.knowledge_page":
            return service.get_page(_required_string_param(request, "page_id")).as_dict()
        server._begin_workspace_mutation(request, cancel_event)
        return service.save_page(
            page_id=_optional_page_id(request),
            kind=_required_string_param(request, "kind"),
            title=_required_string_param(request, "title"),
            content_markdown=_required_markdown_param(request),
        ).as_dict()


def _optional_page_id(request: DesktopRequest) -> str | None:
    from openkb.desktop_engine import DesktopRequestError

    value = request.params.get("page_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DesktopRequestError(
            "invalid_params", f"{request.method} page_id must be a non-empty string when provided."
        )
    return value


def _required_markdown_param(request: DesktopRequest) -> str:
    from openkb.desktop_engine import DesktopRequestError

    value = request.params.get("content_markdown")
    if not isinstance(value, str):
        raise DesktopRequestError(
            "invalid_params", f"{request.method} requires content_markdown as a string."
        )
    return value
