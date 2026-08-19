"""Read-only Desktop Engine requests for the knowledge reconciliation queue."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_knowledge_reconciliation_resolution import (
    DesktopKnowledgeReconciliationResolutionService,
)
from openkb.desktop_missing_sources import DesktopMissingSourceService

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_knowledge_reconciliation_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Expose and commit review choices without teaching transport domain rules."""
    from openkb.desktop_engine import DesktopRequestError

    with server._workspace_requests_lock:
        if cancel_event is not None and cancel_event.is_set():
            raise DesktopRequestError("request_cancelled", "Desktop Bridge request was cancelled.")
        active = server._workspace.active()
        if active is None:
            if request.method == "workbench.knowledge_reconciliation_conflicts":
                return {"conflicts": []}
            if request.method == "workbench.knowledge_reconciliation_missing_sources":
                return {"candidates": []}
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before reviewing knowledge conflicts.",
            )
        kb_dir = Path(active.kb_dir)
        missing_sources = DesktopMissingSourceService(kb_dir)
        if request.method == "workbench.knowledge_reconciliation_missing_sources":
            return {
                "candidates": [
                    candidate.as_dict() for candidate in missing_sources.list_candidates()
                ]
            }
        if request.method == "workbench.bind_knowledge_reconciliation_missing_source":
            server._begin_workspace_mutation(request, cancel_event)
            return missing_sources.bind(
                _required_string(request, "candidate_id"),
                _required_string(request, "evidence_id"),
            ).as_dict()
        if request.method == "workbench.dismiss_knowledge_reconciliation_missing_sources":
            missing_candidate_ids = _required_candidate_ids(request)
            server._begin_workspace_mutation(request, cancel_event)
            return missing_sources.dismiss(missing_candidate_ids).as_dict()
        service = DesktopKnowledgeReconciliationService(kb_dir)
        if request.method == "workbench.knowledge_reconciliation_conflicts":
            return {"conflicts": [conflict.as_dict() for conflict in service.list_conflicts()]}
        if request.method == "workbench.stage_knowledge_reconciliation_decisions":
            candidate_ids = request.params.get("candidate_ids")
            if (
                not isinstance(candidate_ids, list)
                or not candidate_ids
                or any(
                    not isinstance(candidate_id, str) or not candidate_id
                    for candidate_id in candidate_ids
                )
            ):
                raise DesktopRequestError(
                    "invalid_params",
                    "Knowledge reconciliation staging requires non-empty candidate_ids.",
                )
            decision = request.params.get("decision")
            if decision is not None and not isinstance(decision, str):
                raise DesktopRequestError(
                    "invalid_params",
                    "Knowledge reconciliation decision must be a supported review action.",
                )
            if decision not in {
                None,
                "publish_incoming",
                "keep_current",
                "keep_draft",
                "apply_incoming",
                "replace_draft",
                "manual_merge",
            }:
                raise DesktopRequestError(
                    "invalid_params",
                    "Knowledge reconciliation decision must be a supported review action.",
                )
            manual_merge_content = request.params.get("manual_merge_content")
            if manual_merge_content is not None and not isinstance(manual_merge_content, str):
                raise DesktopRequestError(
                    "invalid_params",
                    "Knowledge reconciliation manual_merge_content must be Markdown text.",
                )
            server._begin_workspace_mutation(request, cancel_event)
            conflicts = DesktopKnowledgeReconciliationResolutionService(kb_dir).stage_decisions(
                tuple(candidate_ids),
                decision,
                manual_merge_content=manual_merge_content,
            )
            return {"conflicts": [conflict.as_dict() for conflict in conflicts]}
        if request.method == "workbench.commit_knowledge_reconciliation_decisions":
            server._begin_workspace_mutation(request, cancel_event)
            return (
                DesktopKnowledgeReconciliationResolutionService(kb_dir)
                .commit_staged_decisions()
                .as_dict()
            )
        raise DesktopRequestError(
            "method_not_found", f"Unknown reconciliation method: {request.method}"
        )


def _required_string(request: DesktopRequest, key: str) -> str:
    from openkb.desktop_engine import DesktopRequestError

    value = request.params.get(key)
    if not isinstance(value, str) or not value:
        raise DesktopRequestError(
            "invalid_params", f"{request.method} requires a non-empty {key}."
        )
    return value


def _required_candidate_ids(request: DesktopRequest) -> tuple[str, ...]:
    from openkb.desktop_engine import DesktopRequestError

    value = request.params.get("candidate_ids")
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(candidate_id, str) or not candidate_id for candidate_id in value)
    ):
        raise DesktopRequestError(
            "invalid_params", "Missing Source dismissal requires non-empty candidate_ids."
        )
    return tuple(value)
