"""Grounded-answer dispatch kept separate from the Desktop Engine protocol loop."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.desktop_grounded_answer import DesktopGroundedAnswerService

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_grounded_answer_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Answer against one stable active KB without making model work a mutation."""
    from openkb.desktop_engine import DesktopRequestError, _required_string_param

    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None:
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before asking a question.",
            )
        kb_dir = Path(active.kb_dir)
    service = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=server._model_gateway_factory(kb_dir, None),
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
    if request.method == "workbench.ask_grounded":
        answer = service.answer(
            _required_string_param(request, "question"),
            on_delta=on_delta,
            is_cancelled=is_cancelled,
        )
    else:
        answer = service.retry(
            _required_string_param(request, "answer_id"),
            on_delta=on_delta,
            is_cancelled=is_cancelled,
        )
    return answer.as_dict()
