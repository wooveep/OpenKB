"""Desktop Engine route for current-KB command-palette search."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from openkb.desktop_global_search import search_desktop_knowledge_base

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_global_search_request(
    server: DesktopEngineServer, request: DesktopRequest
) -> dict[str, object]:
    from openkb.desktop_engine import DesktopRequestError

    raw_query = request.params.get("query")
    if not isinstance(raw_query, str):
        raise DesktopRequestError(
            "invalid_params",
            "Global Search query must be a string.",
        )
    query = raw_query.strip()
    active = server._workspace.active()
    if active is None:
        raise DesktopRequestError(
            "no_active_knowledge_base",
            "Open a Desktop Knowledge Base before searching.",
        )
    return search_desktop_knowledge_base(Path(active.kb_dir), query)
