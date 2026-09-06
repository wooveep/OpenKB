"""Exact-contract dispatch and explicit retry scoping for Import Analysis calls."""

from __future__ import annotations

from threading import Lock

from openkb.importing.artifacts import DesktopImportError
from openkb.importing.control import DesktopImportControl
from openkb.importing.model_call import run_import_model_call
from openkb.importing.model_ledger import DesktopImportModelLedger
from openkb.importing.store import DesktopImportStore, ImportJobState
from openkb.models.gateway import (
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelResult,
)
from openkb.models.result_failure import (
    authorize_model_operation_retry,
    mark_model_operation_ready,
    model_operation_dispatch_allowed,
    revoke_model_operation_retry_scope,
)

_ANALYSIS_OPERATIONS = frozenset(
    {
        "knowledge_fact_harvest",
        "knowledge_analysis",
        "knowledge_analysis_batch",
        "knowledge_analysis_merge",
        "structured_output_repair",
    }
)


class DesktopImportAnalysisDispatcher:
    """Gate actual Import requests and own one recovery action's retry round."""

    def __init__(
        self,
        *,
        store: DesktopImportStore,
        ledger: DesktopImportModelLedger,
        control: DesktopImportControl,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._control = control
        self._retry_scope: str | None = None
        self._authorized_contracts: set[tuple[str, str, str]] = set()
        self._lock = Lock()

    def begin_recovery(self, job_id: str) -> None:
        with self._lock:
            self._retry_scope = f"import_recovery:{job_id}"
            self._authorized_contracts.clear()

    def end_recovery(self) -> None:
        with self._lock:
            retry_scope = self._retry_scope
            self._retry_scope = None
            self._authorized_contracts.clear()
        if retry_scope is not None:
            revoke_model_operation_retry_scope(self._store.kb_dir, retry_scope)

    def run(
        self,
        *,
        gateway: DesktopModelGateway,
        state: ImportJobState,
        stage: str,
        request: DesktopModelRequest,
    ) -> DesktopModelResult:
        retry_scope = self._authorize_retry(gateway, request)
        if request.operation in _ANALYSIS_OPERATIONS and not model_operation_dispatch_allowed(
            self._store.kb_dir,
            gateway,
            operation=request.operation,
            retry_scope=retry_scope,
            capability_identity=request.capability_identity,
            prompt_contract_digest=request.prompt_contract_digest,
        ):
            raise DesktopImportError(
                "model_operation_suspended",
                f"The {request.operation} contract is suspended for this exact Analysis profile.",
                suggested_action=(
                    "Correct the model configuration, then explicitly recover this import."
                ),
            )
        return run_import_model_call(
            gateway=gateway,
            ledger=self._ledger,
            store=self._store,
            state=state,
            stage=stage,
            request=request,
            is_cancelled=lambda: self._control.action is not None,
        )

    def mark_ready(
        self,
        gateway: DesktopModelGateway,
        request: DesktopModelRequest,
    ) -> None:
        """Mark the exact persisted-plan request after domain validation."""
        mark_model_operation_ready(
            self._store.kb_dir,
            gateway,
            operation=request.operation,
            capability_identity=request.capability_identity,
            prompt_contract_digest=request.prompt_contract_digest,
        )

    def _authorize_retry(
        self,
        gateway: DesktopModelGateway,
        request: DesktopModelRequest,
    ) -> str | None:
        with self._lock:
            retry_scope = self._retry_scope
            key = (
                request.operation,
                request.capability_identity or "",
                request.prompt_contract_digest or "",
            )
            if retry_scope is not None and key not in self._authorized_contracts:
                authorize_model_operation_retry(
                    self._store.kb_dir,
                    gateway,
                    operation=request.operation,
                    retry_scope=retry_scope,
                    capability_identity=request.capability_identity,
                    prompt_contract_digest=request.prompt_contract_digest,
                )
                self._authorized_contracts.add(key)
            return retry_scope
