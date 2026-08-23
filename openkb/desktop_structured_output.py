"""One shared validation boundary with at most one evidence-bound repair call."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from openkb.desktop_model_gateway import DesktopModelRequest, DesktopModelResult
from openkb.desktop_prompt_contracts import prompt_contract_for

ValidatedValue = TypeVar("ValidatedValue")
StructuredInvoker = Callable[[DesktopModelRequest], DesktopModelResult]
StructuredValidator = Callable[[str], ValidatedValue]


class DesktopStructuredOutputInvalidError(ValueError):
    """The initial output and its sole automatic repair both failed validation."""

    def __init__(self, *, attempt_count: int) -> None:
        super().__init__("The model returned invalid structured output after one automatic repair.")
        self.attempt_count = attempt_count


@dataclass(frozen=True)
class DesktopValidatedStructuredOutput(Generic[ValidatedValue]):
    result: DesktopModelResult
    value: ValidatedValue
    repaired: bool


def run_structured_output(
    *,
    operation: str,
    document_name: str,
    source_material: str,
    invoke: StructuredInvoker,
    validate: StructuredValidator[ValidatedValue],
    contract_snapshot: dict[str, object] | None = None,
    repair_contract_snapshot: dict[str, object] | None = None,
) -> DesktopValidatedStructuredOutput[ValidatedValue]:
    """Validate the original result, then make exactly one separately tracked repair call."""
    contract = prompt_contract_for(operation)
    active_snapshot = contract_snapshot or contract.snapshot()
    output_schema = active_snapshot.get("output_schema")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise ValueError("Prompt Contract output schema is invalid.")
    generation_parameters = active_snapshot.get("generation_parameters")
    if not isinstance(generation_parameters, dict):
        raise ValueError("Prompt Contract generation parameters are invalid.")
    contract_version = active_snapshot.get("version")
    if not isinstance(contract_version, str):
        raise ValueError("Prompt Contract version is invalid.")
    initial_request = DesktopModelRequest(
        operation,
        document_name,
        source_material,
        response_schema=output_schema,
        response_schema_name=_schema_name(contract_version),
        generation_parameters=dict(generation_parameters),
        prompt_contract_digest=_snapshot_digest(active_snapshot),
        prompt_contract_version=contract_version,
        prompt_contract_snapshot=active_snapshot,
    )
    initial = invoke(initial_request)
    try:
        value = validate(normalize_structured_output(initial.content))
    except Exception as first_error:
        repair_contract = prompt_contract_for("structured_output_repair")
        repair_snapshot = repair_contract_snapshot or repair_contract.snapshot()
        repair_generation = repair_snapshot.get("generation_parameters")
        repair_version = repair_snapshot.get("version")
        if not isinstance(repair_generation, dict) or not isinstance(repair_version, str):
            raise ValueError("Structured Output Repair Prompt Contract is invalid.")
        repair_request = DesktopModelRequest(
            "structured_output_repair",
            document_name,
            _repair_input(
                operation=operation,
                schema=output_schema,
                validation_error=first_error,
                invalid_result=initial.content,
                source_material=source_material,
            ),
            response_schema=output_schema,
            response_schema_name=_schema_name(contract_version),
            generation_parameters=dict(repair_generation),
            prompt_contract_digest=_snapshot_digest(repair_snapshot),
            prompt_contract_version=repair_version,
            prompt_contract_snapshot=repair_snapshot,
        )
        repaired = invoke(repair_request)
        try:
            value = validate(normalize_structured_output(repaired.content))
        except Exception as second_error:
            raise DesktopStructuredOutputInvalidError(
                attempt_count=initial.attempt_count + repaired.attempt_count
            ) from second_error
        return DesktopValidatedStructuredOutput(repaired, value, True)
    return DesktopValidatedStructuredOutput(initial, value, False)


def normalize_structured_output(content: str) -> str:
    """Remove a single Markdown transport fence without inventing any JSON content."""
    normalized = content.strip()
    if not normalized.startswith("```"):
        return normalized
    lines = normalized.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return normalized
    return "\n".join(lines[1:-1]).strip()


def _repair_input(
    *,
    operation: str,
    schema: dict[str, object] | None,
    validation_error: Exception,
    invalid_result: str,
    source_material: str,
) -> str:
    return json.dumps(
        {
            "operation": operation,
            "output_schema": schema,
            "validation_errors": [_safe_validation_error(validation_error)],
            "invalid_result": invalid_result,
            "evidence_bound_source_material": source_material,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _safe_validation_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    return message[:1_000] or type(error).__name__


def _schema_name(version: str) -> str:
    return version.replace(".", "_").replace("-", "_")[:64]


def _snapshot_digest(snapshot: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
