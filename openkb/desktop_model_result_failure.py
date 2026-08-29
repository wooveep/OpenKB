"""Convert a locally invalid structured result into a terminal Model Result Failure."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_gateway import (
    MODEL_RESULT_FAILURE_CODES,
    DesktopModelCallError,
    DesktopModelFailure,
    DesktopModelRequest,
    DesktopModelResult,
    has_deferred_model_result_lifecycle,
    invalidate_analysis_capability,
    invalidate_corroborated_analysis_capability,
    record_model_result_failure,
)
from openkb.desktop_model_operation_state import (
    DesktopModelOperationContractStore,
)
from openkb.desktop_model_operation_state import (
    authorize_model_operation_retry_in as _authorize_model_operation_retry_in,
)
from openkb.desktop_model_operation_state import (
    revoke_model_operation_retry_scope_in as _revoke_model_operation_retry_scope_in,
)
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    DesktopValidatedStructuredOutput,
)

ValidatedValue = TypeVar("ValidatedValue")


class DesktopModelOperationSuspendedError(RuntimeError):
    """A local dispatch gate rejected an unchanged suspended contract."""

    def __init__(self, operation: str) -> None:
        super().__init__(f"The {operation} model operation contract is suspended.")
        self.operation = operation


@dataclass(frozen=True)
class DesktopModelOperationCompletionAuthority:
    """Choose ordinary or revision-bound readiness without an unsafe default."""

    retry_scope: str | None

    def __post_init__(self) -> None:
        if self.retry_scope is not None and not self.retry_scope:
            raise ValueError("Model operation retry scope must not be empty.")

    @classmethod
    def ordinary(cls) -> DesktopModelOperationCompletionAuthority:
        return cls(None)

    @classmethod
    def retry(cls, retry_scope: str) -> DesktopModelOperationCompletionAuthority:
        return cls(retry_scope)

    @classmethod
    def for_retry_scope(
        cls, retry_scope: str | None
    ) -> DesktopModelOperationCompletionAuthority:
        return cls.ordinary() if retry_scope is None else cls.retry(retry_scope)


def is_model_result_failure(failure_code: str) -> bool:
    """Return whether one stable code represents a successful-but-unusable result."""
    return failure_code in MODEL_RESULT_FAILURE_CODES


def structured_model_result_failure(
    error: DesktopStructuredOutputInvalidError,
    *,
    suggested_action: str,
) -> DesktopModelCallError:
    """Retain safe final-call metadata while discarding both invalid result bodies."""
    result = error.final_result
    return DesktopModelCallError(
        result.call_id,
        DesktopModelFailure(
            "model_response_invalid",
            str(error),
            suggested_action,
            False,
        ),
        error.attempt_count,
        observations=result.observations,
        usage=result.usage,
        provider_request_id=result.provider_request_id,
        failure_event_id=error.failure_event_id,
        diagnostic_context=error.initial_result.diagnostic_context,
    )


def invalidate_structured_model_result(
    gateway: object,
    error: DesktopStructuredOutputInvalidError,
) -> None:
    """Correct usage and invalidate the exact profile at one shared consumer boundary."""
    failure = record_structured_model_result_failure(gateway, error)
    invalidate_analysis_capability(gateway, failure.failure.code, failure.failure.reason)


def record_structured_model_result_failure(
    gateway: object,
    error: DesktopStructuredOutputInvalidError,
) -> DesktopModelCallError:
    """Correct usage for an operation-local invalid result without changing role readiness."""
    failure = structured_model_result_failure(
        error,
        suggested_action=(
            "Run an explicit Analysis capability check before retrying this operation."
        ),
    )
    if not has_deferred_model_result_lifecycle(error.final_result):
        record_model_result_failure(gateway, failure.call_id, failure.failure.code)
    return failure


def suspend_structured_model_operation(
    kb_dir: Path,
    gateway: object,
    error: DesktopStructuredOutputInvalidError,
    *,
    operation: str,
    failure_code: str,
    reason: str,
    failure_stage: str = "domain_validation",
) -> DesktopModelCallError:
    """Record one invalid result and suspend only its operation contract."""
    failure = record_structured_model_result_failure(gateway, error)
    initial_context = error.initial_result.diagnostic_context
    initial_identity = _mapping_string(initial_context, "capability_identity")
    initial_digest = _mapping_string(initial_context, "prompt_contract_digest")
    suspend_model_operation_contract(
        kb_dir,
        gateway,
        operation=operation,
        failure_code=failure_code,
        reason=reason,
        failure_stage=failure_stage,
        capability_identity=initial_identity,
        prompt_contract_digest=initial_digest,
    )
    final_context = error.final_result.diagnostic_context
    final_operation = _mapping_string(final_context, "operation")
    if final_operation == "structured_output_repair":
        suspend_model_operation_contract(
            kb_dir,
            gateway,
            operation=final_operation,
            failure_code="model_response_invalid",
            reason="The Structured Output Repair response could not be validated.",
            failure_stage=failure_stage,
            capability_identity=_mapping_string(final_context, "capability_identity"),
            prompt_contract_digest=_mapping_string(
                final_context,
                "prompt_contract_digest",
            ),
        )
    return failure


def suspend_model_operation_contract(
    kb_dir: Path,
    gateway: object,
    *,
    operation: str,
    failure_code: str,
    reason: str,
    failure_stage: str = "domain_validation",
    failure_signature: str | None = None,
    capability_identity: str | None = None,
    prompt_contract_digest: str | None = None,
) -> int:
    """Suspend one operation contract when an execution profile is available."""
    key = _operation_contract_key(
        gateway,
        operation,
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
    )
    if key is None:
        return 0
    identity, digest = key
    return DesktopModelOperationContractStore(kb_dir).suspend(
        operation=operation,
        capability_identity=identity,
        prompt_contract_digest=digest,
        failure_code=failure_code,
        reason=reason,
        failure_stage=failure_stage,
        failure_signature=failure_signature,
    )


def mark_model_operation_ready(
    kb_dir: Path,
    gateway: object,
    *,
    operation: str,
    capability_identity: str | None = None,
    prompt_contract_digest: str | None = None,
) -> None:
    """Clear a suspension only after this exact contract validates successfully."""
    key = _operation_contract_key(
        gateway,
        operation,
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
    )
    if key is None:
        return
    identity, digest = key
    DesktopModelOperationContractStore(kb_dir).mark_ready(
        operation=operation,
        capability_identity=identity,
        prompt_contract_digest=digest,
    )


def model_operation_dispatch_possible(
    kb_dir: Path,
    gateway: object,
    *,
    operation: str,
    retry_scope: str | None = None,
    capability_identity: str | None = None,
    prompt_contract_digest: str | None = None,
) -> bool:
    """Check an exact contract without consuming a scoped explicit-retry permit."""
    key = _operation_contract_key(
        gateway,
        operation,
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
    )
    if key is None:
        return True
    identity, digest = key
    return DesktopModelOperationContractStore(kb_dir).dispatch_possible(
        operation=operation,
        capability_identity=identity,
        prompt_contract_digest=digest,
        retry_scope=retry_scope,
    )


def model_operation_dispatch_allowed(
    kb_dir: Path,
    gateway: object,
    *,
    operation: str,
    retry_scope: str | None = None,
    capability_identity: str | None = None,
    prompt_contract_digest: str | None = None,
) -> bool:
    """Join the matching explicit-retry round if this contract is suspended."""
    key = _operation_contract_key(
        gateway,
        operation,
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
    )
    if key is None:
        return True
    identity, digest = key
    return DesktopModelOperationContractStore(kb_dir).claim_dispatch(
        operation=operation,
        capability_identity=identity,
        prompt_contract_digest=digest,
        retry_scope=retry_scope,
    )


def require_model_operation_dispatch(
    kb_dir: Path,
    gateway: object,
    request: DesktopModelRequest,
    *,
    retry_scope: str | None = None,
) -> None:
    """Claim the exact request contract immediately before provider dispatch."""
    if not model_operation_dispatch_allowed(
        kb_dir,
        gateway,
        operation=request.operation,
        retry_scope=retry_scope,
        capability_identity=request.capability_identity,
        prompt_contract_digest=request.prompt_contract_digest,
    ):
        raise DesktopModelOperationSuspendedError(request.operation)


def mark_model_result_operation_ready(
    kb_dir: Path,
    gateway: object,
    result: DesktopModelResult,
    *,
    authority: DesktopModelOperationCompletionAuthority,
) -> None:
    """Mark only the exact request whose result passed local/domain validation."""
    operation = result.diagnostic_context.get("operation")
    if not isinstance(operation, str) or not operation:
        return
    capability_identity = result.diagnostic_context.get("capability_identity")
    prompt_contract_digest = result.diagnostic_context.get("prompt_contract_digest")
    identity = capability_identity if isinstance(capability_identity, str) else None
    digest = prompt_contract_digest if isinstance(prompt_contract_digest, str) else None
    key = _operation_contract_key(
        gateway,
        operation,
        capability_identity=identity,
        prompt_contract_digest=digest,
    )
    if key is None:
        return
    resolved_identity, resolved_digest = key
    if authority.retry_scope is not None:
        DesktopModelOperationContractStore(kb_dir).mark_ready_for_retry(
            operation=operation,
            capability_identity=resolved_identity,
            prompt_contract_digest=resolved_digest,
            retry_scope=authority.retry_scope,
        )
        return
    DesktopModelOperationContractStore(kb_dir).mark_ready_unless_suspended(
        operation=operation,
        capability_identity=resolved_identity,
        prompt_contract_digest=resolved_digest,
    )


def mark_structured_output_operations_ready(
    kb_dir: Path,
    gateway: object,
    output: DesktopValidatedStructuredOutput[ValidatedValue],
    *,
    authority: DesktopModelOperationCompletionAuthority,
) -> None:
    """Mark the parent and, when used, its bound repair contract after validation."""
    if output.initial_result is not None:
        mark_model_result_operation_ready(
            kb_dir,
            gateway,
            output.initial_result,
            authority=authority,
        )
    mark_model_result_operation_ready(
        kb_dir,
        gateway,
        output.result,
        authority=authority,
    )


def authorize_model_operation_retry(
    kb_dir: Path,
    gateway: object,
    *,
    operation: str,
    retry_scope: str,
    capability_identity: str | None = None,
    prompt_contract_digest: str | None = None,
) -> bool:
    """Create one durable, scoped permit while leaving suspension intact."""
    key = _operation_contract_key(
        gateway,
        operation,
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
    )
    if key is None:
        return False
    identity, digest = key
    return DesktopModelOperationContractStore(kb_dir).authorize_retry(
        operation=operation,
        capability_identity=identity,
        prompt_contract_digest=digest,
        retry_scope=retry_scope,
    )


def authorize_model_operation_retry_group(
    kb_dir: Path,
    gateway: object,
    *,
    retry_scope: str,
    contracts: tuple[tuple[str, str | None], ...],
) -> None:
    """Atomically bind a non-task user action to its observed exact contracts."""
    resolved = _resolved_retry_contracts(gateway, contracts)
    DesktopModelOperationContractStore(kb_dir).authorize_retry_group(
        retry_scope=retry_scope,
        contracts=resolved,
    )


def authorize_model_operation_retry_group_in(
    connection: sqlite3.Connection,
    gateway: object,
    *,
    retry_scope: str,
    contracts: tuple[tuple[str, str | None], ...],
) -> None:
    """Bind an action's exact contracts inside the task publication transaction."""
    for operation, capability_identity, digest in _resolved_retry_contracts(
        gateway, contracts
    ):
        _authorize_model_operation_retry_in(
            connection,
            operation=operation,
            capability_identity=capability_identity,
            prompt_contract_digest=digest,
            retry_scope=retry_scope,
        )


def _resolved_retry_contracts(
    gateway: object,
    contracts: tuple[tuple[str, str | None], ...],
) -> tuple[tuple[str, str, str], ...]:
    resolved: list[tuple[str, str, str]] = []
    for operation, prompt_contract_digest in contracts:
        key = _operation_contract_key(
            gateway,
            operation,
            capability_identity=None,
            prompt_contract_digest=prompt_contract_digest,
        )
        if key is not None:
            resolved.append((operation, key[0], key[1]))
    return tuple(resolved)


def revoke_model_operation_retry_scope(kb_dir: Path, retry_scope: str) -> None:
    """End one explicit retry action and discard any unused contract permits."""
    DesktopModelOperationContractStore(kb_dir).revoke_retry_scope(retry_scope)


def revoke_model_operation_retry_scope_in(
    connection: sqlite3.Connection, retry_scope: str
) -> None:
    """Revoke a retry action in the caller's task-state transaction."""
    _revoke_model_operation_retry_scope_in(connection, retry_scope)


def suspend_model_call_operation(
    kb_dir: Path,
    gateway: object,
    error: DesktopModelCallError,
    *,
    operation: str,
    capability_identity: str | None = None,
    prompt_contract_digest: str | None = None,
) -> None:
    """Classify one call failure and invalidate shared evidence only when proven."""
    code = error.failure.code
    if code in {"model_authentication_failed", "model_configuration_invalid"}:
        suspend_model_operation_contract(
            kb_dir,
            gateway,
            operation=operation,
            failure_code=code,
            reason=error.failure.reason,
            failure_stage="confirmed_shared_protocol",
            failure_signature=_protocol_failure_signature(code),
            capability_identity=capability_identity,
            prompt_contract_digest=prompt_contract_digest,
        )
        _invalidate_exact_shared_capability(
            kb_dir,
            gateway,
            capability_identity=capability_identity,
            failure_code=code,
            reason=error.failure.reason,
            corroborated=False,
        )
        return
    if code in MODEL_RESULT_FAILURE_CODES:
        signature = _protocol_failure_signature(code)
        independent_operations = suspend_model_operation_contract(
            kb_dir,
            gateway,
            operation=operation,
            failure_code=code,
            reason=error.failure.reason,
            failure_stage="uncertain_shared_protocol",
            failure_signature=signature,
            capability_identity=capability_identity,
            prompt_contract_digest=prompt_contract_digest,
        )
        if independent_operations >= 2:
            _invalidate_exact_shared_capability(
                kb_dir,
                gateway,
                capability_identity=capability_identity,
                failure_code=code,
                reason=error.failure.reason,
                corroborated=True,
            )
        return
    suspend_model_operation_contract(
        kb_dir,
        gateway,
        operation=operation,
        failure_code=code,
        reason=error.failure.reason,
        failure_stage="provider_transport",
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
    )


def suspend_analysis_operation_failure(
    kb_dir: Path,
    gateway: object,
    error: DesktopModelCallError,
) -> None:
    """Classify an Analysis workload failure using its content-free request context."""
    operation_value = error.diagnostic_context.get("operation")
    operation = operation_value if isinstance(operation_value, str) else "knowledge_analysis"
    capability_identity = _context_string(error, "capability_identity")
    prompt_contract_digest = _context_string(error, "prompt_contract_digest")
    if isinstance(error.__cause__, DesktopStructuredOutputInvalidError):
        suspend_model_operation_contract(
            kb_dir,
            gateway,
            operation=operation,
            failure_code=error.failure.code,
            reason=error.failure.reason,
            failure_stage="domain_validation",
            capability_identity=capability_identity,
            prompt_contract_digest=prompt_contract_digest,
        )
        _suspend_parent_dependency(kb_dir, gateway, error, capability_identity)
        return
    suspend_model_call_operation(
        kb_dir,
        gateway,
        error,
        operation=operation,
        capability_identity=capability_identity,
        prompt_contract_digest=prompt_contract_digest,
    )
    _suspend_parent_dependency(kb_dir, gateway, error, capability_identity)


def _protocol_failure_signature(failure_code: str) -> str:
    return hashlib.sha256(
        f"openkb.shared-protocol-failure.v1:{failure_code}".encode("utf-8")
    ).hexdigest()


def _operation_contract_identity(gateway: object, operation: str) -> str | None:
    profile_factory = getattr(gateway, "execution_profile_for_operation", None)
    if not callable(profile_factory):
        return None
    try:
        profile = profile_factory(operation)
    except (TypeError, ValueError):
        return None
    shared_profile = getattr(profile, "capability_evidence_profile", profile)
    identity = getattr(shared_profile, "identity", None)
    return identity if isinstance(identity, str) and identity else None


def _operation_contract_key(
    gateway: object,
    operation: str,
    *,
    capability_identity: str | None,
    prompt_contract_digest: str | None,
) -> tuple[str, str] | None:
    identity = capability_identity or _operation_contract_identity(gateway, operation)
    if identity is None:
        return None
    digest = prompt_contract_digest or prompt_contract_for(operation).digest
    return identity, digest


def _context_string(error: DesktopModelCallError, key: str) -> str | None:
    return _mapping_string(error.diagnostic_context, key)


def _mapping_string(mapping: dict[str, object], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _invalidate_exact_shared_capability(
    kb_dir: Path,
    gateway: object,
    *,
    capability_identity: str | None,
    failure_code: str,
    reason: str,
    corroborated: bool,
) -> None:
    current_identity = _operation_contract_identity(gateway, "knowledge_analysis")
    if capability_identity is not None and capability_identity != current_identity:
        DesktopModelCapabilityStore(kb_dir).invalidate_identity(
            capability_identity,
            failure_code=failure_code,
            reason=reason,
        )
        return
    if corroborated:
        invalidate_corroborated_analysis_capability(gateway, failure_code, reason)
    else:
        invalidate_analysis_capability(gateway, failure_code, reason)


def _suspend_parent_dependency(
    kb_dir: Path,
    gateway: object,
    error: DesktopModelCallError,
    capability_identity: str | None,
) -> None:
    if _context_string(error, "operation") != "structured_output_repair":
        return
    parent_operation = _context_string(error, "parent_operation")
    parent_digest = _context_string(error, "parent_prompt_contract_digest")
    if parent_operation is None or parent_digest is None:
        return
    suspend_model_operation_contract(
        kb_dir,
        gateway,
        operation=parent_operation,
        failure_code="structured_output_repair_unavailable",
        reason="The required Structured Output Repair contract did not validate.",
        failure_stage="dependent_operation",
        capability_identity=capability_identity,
        prompt_contract_digest=parent_digest,
    )
