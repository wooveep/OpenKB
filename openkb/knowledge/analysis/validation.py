"""Aggregate independently detectable Knowledge Analysis validation failures."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from typing import Protocol, TypeVar

from openkb.importing.artifacts import DesktopImportError

_INVALID_RESPONSE_ACTION = "Choose a model that can return the required Knowledge Analysis JSON."
_MAX_SOURCE_ERROR_PATHS = 8
_ValidatedValue = TypeVar("_ValidatedValue")


class _HasEvidenceSources(Protocol):
    @property
    def source_evidence_ids(self) -> tuple[str, ...]: ...


class _HasClaims(Protocol):
    @property
    def claims(self) -> tuple[_HasEvidenceSources, ...]: ...


class KnowledgeAnalysisValidationError(DesktopImportError):
    """One or more independently detectable response validation failures."""

    validation_errors: tuple[str, ...]

    def __init__(self, validation_errors: tuple[str, ...]) -> None:
        unique_errors = tuple(dict.fromkeys(error for error in validation_errors if error))
        if not unique_errors:
            raise ValueError("Knowledge Analysis validation errors cannot be empty.")
        message = (
            unique_errors[0]
            if len(unique_errors) == 1
            else "Knowledge Analysis returned multiple invalid fields: " + " ".join(unique_errors)
        )
        super().__init__(
            "model_response_invalid",
            message,
            suggested_action=_INVALID_RESPONSE_ACTION,
        )
        self.validation_errors = unique_errors


def invalid_response(message: str) -> KnowledgeAnalysisValidationError:
    return KnowledgeAnalysisValidationError((message,))


def exceeds_limit(value: list[object], maximum: int) -> bool:
    return len(value) > maximum


def json_object_text(content: str) -> str:
    """Remove one complete Markdown fence without repairing provider JSON."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0].strip()
    return stripped


def validated_or_default(
    validate: Callable[[], _ValidatedValue],
    default: _ValidatedValue,
    validation_errors: list[str],
) -> _ValidatedValue:
    """Capture a domain validation failure while allowing sibling fields to be checked."""
    try:
        return validate()
    except DesktopImportError as error:
        messages = (
            error.validation_errors
            if isinstance(error, KnowledgeAnalysisValidationError)
            else (str(error),)
        )
        validation_errors.extend(
            message for message in messages if message not in validation_errors
        )
        return default


def validate_evidence_scope(
    candidate_groups: Iterable[tuple[str, Iterable[_HasClaims]]],
    summary_units: Iterable[_HasEvidenceSources],
    *,
    allowed_evidence_ids: Collection[str],
    scope_label: str,
) -> None:
    """Reject every invented Evidence ID with bounded, actionable repair feedback."""
    locations_by_id: dict[str, list[str]] = {}
    for group_name, candidates in candidate_groups:
        for candidate_index, candidate in enumerate(candidates):
            for claim_index, claim in enumerate(candidate.claims):
                _record_invalid_sources(
                    locations_by_id,
                    claim.source_evidence_ids,
                    f"{group_name}[{candidate_index}].claims[{claim_index}].source_evidence_ids",
                    allowed_evidence_ids,
                )
    for unit_index, unit in enumerate(summary_units):
        _record_invalid_sources(
            locations_by_id,
            unit.source_evidence_ids,
            f"document_summary[{unit_index}].source_evidence_ids",
            allowed_evidence_ids,
        )
    if locations_by_id:
        raise KnowledgeAnalysisValidationError(
            tuple(
                _source_scope_error(evidence_id, paths, scope_label)
                for evidence_id, paths in locations_by_id.items()
            )
        )


def _record_invalid_sources(
    locations_by_id: dict[str, list[str]],
    source_evidence_ids: Iterable[str],
    path: str,
    allowed_evidence_ids: Collection[str],
) -> None:
    for evidence_id in source_evidence_ids:
        if evidence_id not in allowed_evidence_ids:
            locations_by_id.setdefault(evidence_id, []).append(path)


def _source_scope_error(evidence_id: str, paths: list[str], scope_label: str) -> str:
    visible_paths = ", ".join(paths[:_MAX_SOURCE_ERROR_PATHS])
    omitted = len(paths) - _MAX_SOURCE_ERROR_PATHS
    remainder = f", and {omitted} more locations" if omitted > 0 else ""
    return (
        f"Knowledge Analysis Evidence ID {evidence_id!r} is outside the {scope_label} at "
        f"{visible_paths}{remainder}; remove or replace every occurrence using only supplied "
        "Evidence IDs."
    )
