"""Knowledge-page requests separated from the Desktop Engine transport loop."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, cast

from openkb.desktop_knowledge_export import (
    DesktopKnowledgeExportMode,
    DesktopKnowledgeExportService,
)
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
            return {
                "pages": [page.as_dict() for page in service.list_pages()],
                "selected_page_id": service.selected_page_id(),
            }
        if request.method == "workbench.knowledge_page":
            return service.select_page(_required_string_param(request, "page_id")).as_dict()
        if request.method == "workbench.search_knowledge_sources":
            return {
                "sources": [
                    source.as_dict()
                    for source in service.search_sources(_required_string_param(request, "query"))
                ]
            }
        server._begin_workspace_mutation(request, cancel_event)
        if request.method == "workbench.export_knowledge_bundle":
            return DesktopKnowledgeExportService(Path(active.kb_dir)).export(
                Path(_required_string_param(request, "destination")),
                mode=_required_export_mode(request),
            ).as_dict()
        if request.method == "workbench.publish_knowledge_page":
            return service.publish(_required_string_param(request, "page_id")).as_dict()
        if request.method == "workbench.verify_knowledge_page":
            return service.verify(_required_string_param(request, "page_id")).as_dict()
        if request.method == "workbench.set_knowledge_page_stale_after":
            return service.set_stale_after(
                _required_string_param(request, "page_id"),
                _optional_stale_after_param(request),
            ).as_dict()
        if request.method == "workbench.deprecate_knowledge_page":
            return service.deprecate(_required_string_param(request, "page_id")).as_dict()
        if request.method == "workbench.restore_knowledge_page":
            return service.restore(_required_string_param(request, "page_id")).as_dict()
        if request.method == "workbench.permanently_delete_knowledge_page":
            page_id = _required_string_param(request, "page_id")
            service.permanent_delete(
                page_id,
                confirmation_page_id=_required_string_param(request, "confirmation_page_id"),
            )
            return {"page_id": page_id, "deleted": True}
        if request.method == "workbench.bind_knowledge_page_source":
            return service.bind_source(
                _required_string_param(request, "page_id"),
                _required_string_param(request, "claim_text"),
                _required_string_param(request, "evidence_id"),
            ).as_dict()
        return service.save_draft(
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


def _optional_stale_after_param(request: DesktopRequest) -> str | None:
    from openkb.desktop_engine import DesktopRequestError

    value = request.params.get("stale_after")
    if value is None:
        return None
    if not isinstance(value, str):
        raise DesktopRequestError(
            "invalid_params", f"{request.method} stale_after must be a string or null."
        )
    return value


def _required_export_mode(request: DesktopRequest) -> DesktopKnowledgeExportMode:
    from openkb.desktop_engine import DesktopRequestError

    value = request.params.get("mode")
    if value not in {"knowledge_projection", "self_contained"}:
        raise DesktopRequestError(
            "invalid_params", f"{request.method} requires a supported Knowledge Bundle mode."
        )
    return cast(DesktopKnowledgeExportMode, value)
