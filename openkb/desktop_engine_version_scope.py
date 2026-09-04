"""Strict Desktop Engine boundary for optional Version Scope filters."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from openkb.desktop_version_scope import VersionFilter, VersionMode

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopRequest


def optional_version_filter_param(request: DesktopRequest) -> VersionFilter | None:
    from openkb.desktop_engine import DesktopRequestError

    value = request.params.get("version_filter")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DesktopRequestError("invalid_params", "Version filter must be an object.")
    allowed = {"mode", "lineage_ids", "version_labels", "document_ids"}
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise DesktopRequestError("invalid_params", "Version filter has unknown fields.")
    mode_value = value.get("mode")
    if mode_value is not None and mode_value not in {
        "latest",
        "exact",
        "compare",
        "all",
        "unscoped",
    }:
        raise DesktopRequestError("invalid_params", "Choose a supported version filter mode.")
    return VersionFilter(
        mode=cast(VersionMode | None, mode_value),
        lineage_ids=_string_list(value.get("lineage_ids", []), "lineage_ids"),
        version_labels=_string_list(value.get("version_labels", []), "version_labels"),
        document_ids=_string_list(value.get("document_ids", []), "document_ids"),
    )


def _string_list(value: object, name: str) -> tuple[str, ...]:
    from openkb.desktop_engine import DesktopRequestError

    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise DesktopRequestError(
            "invalid_params", f"Version filter {name} must be a list of non-empty strings."
        )
    return tuple(dict.fromkeys(value))
