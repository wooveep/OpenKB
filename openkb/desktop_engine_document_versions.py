"""Desktop Engine handlers for user-confirmed D3 Document Version Candidates."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, cast

from openkb.desktop_document_version_catalog import (
    DocumentLineageDecision,
    DocumentVersionMemberDecision,
    SnapshotKind,
)
from openkb.desktop_document_versions import DesktopDocumentVersionService
from openkb.desktop_version_labels import VersionScheme

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
        if request.method == "workbench.document_version_catalog":
            return asdict(service.catalog_snapshot())
        if request.method == "workbench.document_version_diffs":
            return {
                "diffs": [
                    asdict(diff)
                    for diff in service.list_diffs(_required_string_param(request, "lineage_id"))
                ]
            }
        if request.method == "workbench.confirm_document_lineage":
            server._begin_workspace_mutation(request, cancel_event)
            return asdict(service.confirm_lineage(_lineage_decision_param(request)))
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


def _lineage_decision_param(request: DesktopRequest) -> DocumentLineageDecision:
    from openkb.desktop_engine import DesktopRequestError

    value = request.params.get("decision")
    if not isinstance(value, dict):
        raise DesktopRequestError("invalid_params", "Lineage decision must be an object.")
    _reject_unknown_fields(
        value,
        {
            "display_name",
            "version_scheme",
            "members",
            "current_document_id",
            "aliases",
            "lineage_id",
            "expected_metadata_revisions",
        },
        "lineage decision",
    )
    scheme = _required_string(value, "version_scheme", "lineage decision")
    if scheme not in {"numeric_dotted", "semver", "calendar", "opaque"}:
        raise DesktopRequestError("invalid_params", "Choose a supported version scheme.")
    members_value = value.get("members")
    if not isinstance(members_value, list) or not members_value:
        raise DesktopRequestError(
            "invalid_params", "Lineage decision members must be a non-empty list."
        )
    revisions_value = value.get("expected_metadata_revisions", [])
    if not isinstance(revisions_value, list):
        raise DesktopRequestError("invalid_params", "Expected lineage revisions must be a list.")
    lineage_id = value.get("lineage_id")
    if lineage_id is not None and (not isinstance(lineage_id, str) or not lineage_id):
        raise DesktopRequestError(
            "invalid_params", "Lineage ID must be a non-empty string when provided."
        )
    return DocumentLineageDecision(
        display_name=_required_string(value, "display_name", "lineage decision"),
        version_scheme=cast(VersionScheme, scheme),
        members=tuple(_member_decision(item) for item in members_value),
        current_document_id=_required_string(value, "current_document_id", "lineage decision"),
        aliases=_string_list(value.get("aliases", []), "Lineage aliases"),
        lineage_id=lineage_id,
        expected_metadata_revisions=tuple(_expected_revision(item) for item in revisions_value),
    )


def _member_decision(value: object) -> DocumentVersionMemberDecision:
    from openkb.desktop_engine import DesktopRequestError

    if not isinstance(value, dict):
        raise DesktopRequestError("invalid_params", "Each lineage member must be an object.")
    _reject_unknown_fields(
        value,
        {
            "document_id",
            "version_label",
            "branch_label",
            "predecessor_document_id",
            "snapshot_kind",
            "metadata_origin",
        },
        "lineage member",
    )
    predecessor = value.get("predecessor_document_id")
    if predecessor is not None and (not isinstance(predecessor, str) or not predecessor):
        raise DesktopRequestError(
            "invalid_params", "Member predecessor must be a non-empty string when provided."
        )
    snapshot_kind = value.get("snapshot_kind", "full_snapshot")
    if snapshot_kind not in {"full_snapshot", "delta", "unknown"}:
        raise DesktopRequestError("invalid_params", "Choose a supported snapshot kind.")
    return DocumentVersionMemberDecision(
        document_id=_required_string(value, "document_id", "lineage member"),
        version_label=_required_string(value, "version_label", "lineage member"),
        branch_label=_optional_string(value, "branch_label", "main"),
        predecessor_document_id=predecessor,
        snapshot_kind=cast(SnapshotKind, snapshot_kind),
        metadata_origin=_optional_string(value, "metadata_origin", "user"),
    )


def _expected_revision(value: object) -> tuple[str, int]:
    from openkb.desktop_engine import DesktopRequestError

    if not isinstance(value, dict):
        raise DesktopRequestError(
            "invalid_params", "Each expected lineage revision must be an object."
        )
    _reject_unknown_fields(value, {"lineage_id", "metadata_revision"}, "expected lineage revision")
    revision = value.get("metadata_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise DesktopRequestError(
            "invalid_params", "Expected metadata revision must be a non-negative integer."
        )
    return _required_string(value, "lineage_id", "expected lineage revision"), revision


def _required_string(value: dict[object, object], key: str, owner: str) -> str:
    from openkb.desktop_engine import DesktopRequestError

    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise DesktopRequestError(
            "invalid_params", f"{owner.capitalize()} requires a non-empty {key}."
        )
    return item.strip()


def _optional_string(value: dict[object, object], key: str, default: str) -> str:
    from openkb.desktop_engine import DesktopRequestError

    item = value.get(key, default)
    if not isinstance(item, str) or not item.strip():
        raise DesktopRequestError("invalid_params", f"{key} must be a non-empty string.")
    return item.strip()


def _string_list(value: object, owner: str) -> tuple[str, ...]:
    from openkb.desktop_engine import DesktopRequestError

    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise DesktopRequestError("invalid_params", f"{owner} must be a list of strings.")
    return tuple(dict.fromkeys(value))


def _reject_unknown_fields(value: dict[object, object], allowed: set[str], owner: str) -> None:
    from openkb.desktop_engine import DesktopRequestError

    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise DesktopRequestError("invalid_params", f"{owner.capitalize()} has unknown fields.")
