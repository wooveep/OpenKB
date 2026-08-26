"""One shared validation boundary with at most one evidence-bound repair call."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from openkb.desktop_model_gateway import (
    DesktopModelRequest,
    DesktopModelResult,
    complete_model_result,
    reject_model_result,
)
from openkb.desktop_prompt_contracts import minimal_json_example, prompt_contract_for

ValidatedValue = TypeVar("ValidatedValue")
StructuredInvoker = Callable[[DesktopModelRequest], DesktopModelResult]
StructuredValidator = Callable[[str], ValidatedValue]


class DesktopStructuredOutputInvalidError(ValueError):
    """The initial output and its sole automatic repair both failed validation."""

    def __init__(
        self,
        *,
        initial_result: DesktopModelResult,
        final_result: DesktopModelResult,
    ) -> None:
        super().__init__("The model returned invalid structured output after one automatic repair.")
        self.initial_result = initial_result
        self.final_result = final_result
        self.attempt_count = initial_result.attempt_count + final_result.attempt_count


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
    output_example = active_snapshot.get("output_example")
    if output_example is not None and not isinstance(output_example, dict):
        raise ValueError("Prompt Contract output example is invalid.")
    if output_example is None and output_schema is not None:
        generated_example = minimal_json_example(output_schema)
        output_example = generated_example if isinstance(generated_example, dict) else None
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
        local_validation_required=True,
        response_example=output_example,
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
        reject_model_result(
            initial,
            failure_code="model_response_invalid",
            reason="Local schema validation rejected the model result.",
        )
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
                output_example=output_example,
                validation_error=first_error,
                invalid_result=initial.content,
                source_material=source_material,
            ),
            response_schema=output_schema,
            local_validation_required=True,
            response_example=output_example,
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
            reject_model_result(
                repaired,
                failure_code="model_response_invalid",
                reason="Local schema validation rejected the model result.",
            )
            raise DesktopStructuredOutputInvalidError(
                initial_result=initial,
                final_result=repaired,
            ) from second_error
        complete_model_result(repaired)
        return DesktopValidatedStructuredOutput(repaired, value, True)
    complete_model_result(initial)
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
    output_example: dict[str, object] | None,
    validation_error: Exception,
    invalid_result: str,
    source_material: str,
) -> str:
    return json.dumps(
        {
            "operation": operation,
            "output_schema": schema,
            "output_example": output_example,
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
