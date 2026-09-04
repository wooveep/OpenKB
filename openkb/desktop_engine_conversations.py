"""Conversation dispatch kept separate from the Desktop Engine protocol loop."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.desktop_conversations import DesktopConversationService
from openkb.desktop_engine_model_lifecycle import emit_model_lifecycle
from openkb.desktop_engine_version_scope import optional_version_filter_param
from openkb.desktop_model_terminal import DesktopTerminalModelEvent

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_conversation_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Run conversation work against one stable active knowledge base."""
    from openkb.desktop_engine import DesktopRequestError, _required_string_param

    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None:
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before using conversations.",
            )
        kb_dir = Path(active.kb_dir)
    service = DesktopConversationService(
        kb_dir,
        model_gateway=_interactive_gateway(server._model_gateway_factory(kb_dir, None)),
    )

    if request.method == "workbench.conversations":
        search = request.params.get("search", "")
        if not isinstance(search, str):
            raise DesktopRequestError("invalid_params", "Conversation search must be text.")
        return service.list(search)
    if request.method == "workbench.create_conversation":
        title = request.params.get("title")
        if title is not None and not isinstance(title, str):
            raise DesktopRequestError("invalid_params", "Conversation title must be text.")
        return service.create(title)

    conversation_id = _required_string_param(request, "conversation_id")
    if request.method == "workbench.conversation":
        return service.get(conversation_id)
    if request.method == "workbench.rename_conversation":
        return service.rename(conversation_id, _required_string_param(request, "title"))
    if request.method == "workbench.delete_conversation":
        return service.delete(conversation_id)
    if request.method == "workbench.save_conversation_draft":
        draft_text = request.params.get("draft_text")
        if not isinstance(draft_text, str):
            raise DesktopRequestError("invalid_params", "Conversation draft must be text.")
        return service.save_draft(conversation_id, draft_text)
    if request.method == "workbench.select_answer_version":
        return service.select_answer_version(
            conversation_id,
            _required_string_param(request, "assistant_message_id"),
            _required_string_param(request, "answer_version_id"),
        )

    def on_delta(answer_id: str, delta: str, replace: bool, attempt: int) -> None:
        server._emit_event(
            "answer.delta",
            {
                "request_id": request.request_id,
                "answer_id": answer_id,
                "delta": delta,
                "replace": replace,
                "attempt": attempt,
            },
        )

    is_cancelled = cancel_event.is_set if cancel_event is not None else None

    def on_model_event(event: object) -> None:
        if not isinstance(event, DesktopTerminalModelEvent):
            return
        emit_model_lifecycle(
            server,
            kb_dir=kb_dir,
            request_id=request.request_id,
            event=event,
        )

    if request.method == "workbench.ask_conversation":
        return service.ask(
            conversation_id,
            _required_string_param(request, "question"),
            on_delta=on_delta,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
            version_filter=optional_version_filter_param(request),
        )
    if request.method == "workbench.regenerate_conversation_answer":
        return service.regenerate(
            conversation_id,
            _required_string_param(request, "assistant_message_id"),
            on_delta=on_delta,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
        )
    raise DesktopRequestError("method_not_found", f"Unknown conversation method: {request.method}")


def _interactive_gateway(gateway):
    select_lane = getattr(gateway, "for_lane", None)
    return select_lane("interactive") if callable(select_lane) else gateway
