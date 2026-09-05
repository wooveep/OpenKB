"""One shared validation boundary with at most one evidence-bound repair call."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from openkb.desktop_model_failure_logging import (
    own_structured_model_failure,
    own_unrepaired_structured_model_failure,
)
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
StructuredRepairDecision = Callable[[Exception], bool]


class DesktopStructuredOutputInvalidError(ValueError):
    """Structured output remained invalid under the operation's repair policy."""

    def __init__(
        self,
        *,
        initial_result: DesktopModelResult,
        final_result: DesktopModelResult,
        repair_attempted: bool = True,
        failure_event_id: str | None = None,
    ) -> None:
        message = (
            "The model returned invalid structured output after one automatic repair."
            if repair_attempted
            else "The model returned invalid structured output that was not eligible for repair."
        )
        super().__init__(message)
        self.initial_result = initial_result
        self.final_result = final_result
        self.repair_attempted = repair_attempted
        self.attempt_count = (
            initial_result.attempt_count + final_result.attempt_count
            if repair_attempted
            else initial_result.attempt_count
        )
        self.failure_event_id = failure_event_id


def structured_output_reached_limit(error: DesktopStructuredOutputInvalidError) -> bool:
    """Return whether the provider stopped an attempted result at its output bound."""
    results = (
        (error.initial_result, error.final_result)
        if error.repair_attempted
        else (error.initial_result,)
    )
    return any(
        result.observations is not None and result.observations.output_limit_reached
        for result in results
    )


@dataclass(frozen=True)
class DesktopValidatedStructuredOutput(Generic[ValidatedValue]):
    result: DesktopModelResult
    value: ValidatedValue
    repaired: bool
    initial_result: DesktopModelResult | None = None


def run_structured_output(
    *,
    operation: str,
    document_name: str,
    source_material: str,
    invoke: StructuredInvoker,
    validate: StructuredValidator[ValidatedValue],
    contract_snapshot: dict[str, object] | None = None,
    repair_contract_snapshot: dict[str, object] | None = None,
    should_repair: StructuredRepairDecision | None = None,
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
    if initial.observations is not None and initial.observations.output_limit_reached:
        limit_error = ValueError(
            "The provider stopped at the output limit before structured output completed."
        )
        reject_model_result(
            initial,
            failure_code="model_response_invalid",
            reason="Provider output stopped at its configured limit.",
        )
        failure_event_id = own_unrepaired_structured_model_failure(
            operation=operation,
            document_name=document_name,
            source_material=source_material,
            initial=initial,
            error=limit_error,
        )
        raise DesktopStructuredOutputInvalidError(
            initial_result=initial,
            final_result=initial,
            repair_attempted=False,
            failure_event_id=failure_event_id,
        ) from limit_error
    try:
        value = validate(normalize_structured_output(initial.content))
    except Exception as first_error:
        reject_model_result(
            initial,
            failure_code="model_response_invalid",
            reason="Local schema validation rejected the model result.",
        )
        if should_repair is not None and not should_repair(first_error):
            failure_event_id = own_unrepaired_structured_model_failure(
                operation=operation,
                document_name=document_name,
                source_material=source_material,
                initial=initial,
                error=first_error,
            )
            raise DesktopStructuredOutputInvalidError(
                initial_result=initial,
                final_result=initial,
                repair_attempted=False,
                failure_event_id=failure_event_id,
            ) from first_error
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
            prompt_contract_digest=_repair_contract_digest(
                repair_snapshot,
                parent_operation=operation,
                parent_prompt_contract_digest=_snapshot_digest(active_snapshot),
                output_schema=output_schema,
            ),
            parent_operation=operation,
            parent_prompt_contract_digest=_snapshot_digest(active_snapshot),
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
            failure_event_id = own_structured_model_failure(
                operation=operation,
                document_name=document_name,
                source_material=source_material,
                initial=initial,
                repaired=repaired,
                first_error=first_error,
                second_error=second_error,
            )
            raise DesktopStructuredOutputInvalidError(
                initial_result=initial,
                final_result=repaired,
                failure_event_id=failure_event_id,
            ) from second_error
        complete_model_result(repaired)
        return DesktopValidatedStructuredOutput(repaired, value, True, initial)
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


def structured_output_repair_contract_digest(parent_operation: str) -> str:
    """Return the exact default repair digest bound to one parent contract."""
    parent_contract = prompt_contract_for(parent_operation)
    return _repair_contract_digest(
        prompt_contract_for("structured_output_repair").snapshot(),
        parent_operation=parent_operation,
        parent_prompt_contract_digest=parent_contract.digest,
        output_schema=parent_contract.output_schema,
    )


def _repair_contract_digest(
    repair_snapshot: dict[str, object],
    *,
    parent_operation: str,
    parent_prompt_contract_digest: str,
    output_schema: dict[str, object] | None,
) -> str:
    return _snapshot_digest(
        {
            "repair_contract": repair_snapshot,
            "parent_operation": parent_operation,
            "parent_prompt_contract_digest": parent_prompt_contract_digest,
            "output_schema": output_schema,
        }
    )


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
            "validation_errors": _safe_validation_errors(validation_error),
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


def _safe_validation_errors(error: Exception) -> list[str]:
    validation_errors = getattr(error, "validation_errors", None)
    if not isinstance(validation_errors, tuple) or not all(
        isinstance(message, str) for message in validation_errors
    ):
        return [_safe_validation_error(error)]
    safe_messages: list[str] = []
    for message in validation_errors[:16]:
        normalized = " ".join(message.split())[:1_000]
        if normalized and normalized not in safe_messages:
            safe_messages.append(normalized)
    return safe_messages or [_safe_validation_error(error)]


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
