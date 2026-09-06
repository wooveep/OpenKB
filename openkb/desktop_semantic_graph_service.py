"""Run exhaustive relation analysis over admitted document knowledge identities."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from openkb.desktop_knowledge_graph_store import persist_semantic_graph_interpretation_in
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
)
from openkb.desktop_model_result_failure import (
    DesktopModelOperationCompletionAuthority,
    DesktopModelOperationSuspendedError,
    mark_structured_output_operations_ready,
    model_operation_dispatch_possible,
    require_model_operation_dispatch,
    suspend_analysis_operation_failure,
    suspend_structured_model_operation,
)
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_semantic_graph import (
    SEMANTIC_RELATION_OPERATION,
    SemanticGraphCapacityError,
    SemanticGraphDocument,
    SemanticGraphInterpretation,
    SemanticGraphStoredDataError,
    SemanticRelationBatch,
    SemanticRelationBoundary,
    SemanticRelationInterpretationError,
    load_semantic_graph_input_in,
    merge_semantic_relation_interpretations,
    plan_semantic_relation_batches,
    replace_document_semantic_relations_in,
    semantic_relation_sub_batch,
)
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    DesktopValidatedStructuredOutput,
    run_structured_output,
    structured_output_reached_limit,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

CancellationCallback = Callable[[], bool]
ModelEventCallback = Callable[[object], None]
FailureCallback = Callable[[str, str], None]
PublishOperation = Callable[[sqlite3.Connection], bool]
PublishTransaction = Callable[[PublishOperation], bool]

logger = logging.getLogger(__name__)

_DEFAULT_INPUT_BUDGET_TOKENS = 12_000
_MINIMUM_INPUT_BUDGET_TOKENS = 512
_INVALID_RESPONSE_CODE = "knowledge_graph_response_invalid"
_INVALID_RESPONSE_REASON = "Semantic relation analysis returned an invalid result."


class DesktopSemanticGraphService:
    """Own the only current-epoch relation-analysis path."""

    def __init__(self, kb_dir: Path, *, model_gateway: DesktopModelGateway | None) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)
        self.gateway = model_gateway

    def extract_document_if_admitted(
        self,
        document_id: str,
        *,
        is_cancelled: CancellationCallback | None = None,
        on_model_event: ModelEventCallback | None = None,
        on_failure: FailureCallback | None = None,
        publish_transaction: PublishTransaction | None = None,
        retry_scope: str | None = None,
    ) -> bool:
        """Publish model-labelled relations, or fail without synthesizing semantics."""
        try:
            graph_input = self._input(document_id)
        except (OSError, sqlite3.Error):
            _report_failure(on_failure, "knowledge_graph_extraction_failed")
            return False
        if graph_input.status == "dependency_unavailable" or graph_input.document is None:
            _report_failure(on_failure, "candidate_generation_unavailable")
            return False
        document = graph_input.document
        if _is_cancelled(is_cancelled):
            return False
        if (
            document.claims
            and self.gateway is not None
            and not model_operation_dispatch_possible(
                self.kb_dir,
                self.gateway,
                operation=SEMANTIC_RELATION_OPERATION,
                retry_scope=retry_scope,
            )
        ):
            return False

        outputs: list[DesktopValidatedStructuredOutput[SemanticGraphInterpretation]] = []
        try:
            interpretations: tuple[SemanticGraphInterpretation, ...]
            if not document.claims:
                interpretations = ()
            elif self.gateway is None:
                _report_failure(on_failure, "knowledge_graph_model_unavailable")
                return False
            else:
                batches = plan_semantic_relation_batches(
                    document,
                    input_budget_tokens=self._input_budget_tokens(),
                )
                for batch in batches:
                    if _is_cancelled(is_cancelled):
                        return False
                    outputs.extend(
                        self._run_batch_with_output_limit_recovery(
                            batch,
                            is_cancelled=is_cancelled,
                            on_model_event=on_model_event,
                            retry_scope=retry_scope,
                        )
                    )
                interpretations = tuple(output.value for output in outputs)
            interpretation = merge_semantic_relation_interpretations(document, interpretations)
            published = self._persist(
                document,
                interpretation,
                publish_transaction=publish_transaction,
            )
            if not published:
                return False
            if self.gateway is not None:
                authority = DesktopModelOperationCompletionAuthority.for_retry_scope(retry_scope)
                for output in outputs:
                    mark_structured_output_operations_ready(
                        self.kb_dir,
                        self.gateway,
                        output,
                        authority=authority,
                    )
            return True
        except DesktopModelCancelledError:
            return False
        except DesktopModelOperationSuspendedError:
            _report_failure(on_failure, "model_operation_suspended")
        except DesktopModelCallError as error:
            if self.gateway is not None:
                suspend_analysis_operation_failure(self.kb_dir, self.gateway, error)
            _report_failure(on_failure, error.failure.code, error.failure.reason)
        except DesktopStructuredOutputInvalidError as error:
            if self.gateway is not None:
                suspend_structured_model_operation(
                    self.kb_dir,
                    self.gateway,
                    error,
                    operation=SEMANTIC_RELATION_OPERATION,
                    failure_code=_INVALID_RESPONSE_CODE,
                    reason=_INVALID_RESPONSE_REASON,
                )
            _report_failure(on_failure, _INVALID_RESPONSE_CODE, _INVALID_RESPONSE_REASON)
        except SemanticGraphCapacityError as error:
            _report_failure(on_failure, "knowledge_graph_capacity_exceeded", str(error))
        except SemanticGraphStoredDataError as error:
            _report_failure(on_failure, "knowledge_graph_stored_data_invalid", str(error))
        except (TypeError, ValueError):
            _report_failure(on_failure, _INVALID_RESPONSE_CODE, _INVALID_RESPONSE_REASON)
        except (OSError, sqlite3.Error):
            _report_failure(on_failure, "knowledge_graph_extraction_failed")
        return False

    def _document(self, document_id: str) -> SemanticGraphDocument | None:
        return self._input(document_id).document

    def _input(self, document_id: str):
        connection = self._connect()
        try:
            return load_semantic_graph_input_in(connection, document_id)
        finally:
            connection.close()

    def _run_batch(
        self,
        batch: SemanticRelationBatch,
        *,
        is_cancelled: CancellationCallback | None,
        on_model_event: ModelEventCallback | None,
        retry_scope: str | None,
    ) -> DesktopValidatedStructuredOutput[SemanticGraphInterpretation]:
        if self.gateway is None:
            raise ValueError("Semantic relation model is unavailable.")
        gateway = self.gateway

        def invoke(request: DesktopModelRequest):
            require_model_operation_dispatch(
                self.kb_dir,
                gateway,
                request,
                retry_scope=retry_scope,
            )
            return gateway.analyze(
                request,
                on_event=on_model_event or (lambda _event: None),
                is_cancelled=is_cancelled,
            )

        validation_attempt = 0

        def validate(content: str) -> SemanticGraphInterpretation:
            nonlocal validation_attempt
            validation_attempt += 1
            interpretation = SemanticRelationBoundary.interpret(
                content,
                batch,
                # Every valid edge is independently safe to retain because the
                # boundary rechecks identity and evidence bindings.
                # If an all-invalid initial result is repaired to another
                # all-invalid array, retain an auditable degraded-empty leaf so
                # it cannot erase valid edges from every other document batch.
                reject_partial=False,
                allow_empty_degraded=validation_attempt > 1,
            )
            if interpretation.lifecycle == "failed":
                raise SemanticRelationInterpretationError(interpretation)
            return interpretation

        return run_structured_output(
            operation=SEMANTIC_RELATION_OPERATION,
            document_name=batch.document.document_name,
            source_material=batch.source_material,
            invoke=invoke,
            validate=validate,
            should_repair=lambda error: isinstance(error, SemanticRelationInterpretationError)
            and error.interpretation.repairable,
        )

    def _run_batch_with_output_limit_recovery(
        self,
        batch: SemanticRelationBatch,
        *,
        is_cancelled: CancellationCallback | None,
        on_model_event: ModelEventCallback | None,
        retry_scope: str | None,
    ) -> tuple[DesktopValidatedStructuredOutput[SemanticGraphInterpretation], ...]:
        """Split only provider-truncated batches until every claim has a complete result."""
        try:
            return (
                self._run_batch(
                    batch,
                    is_cancelled=is_cancelled,
                    on_model_event=on_model_event,
                    retry_scope=retry_scope,
                ),
            )
        except DesktopStructuredOutputInvalidError as error:
            if not structured_output_reached_limit(error) or len(batch.claims) <= 1:
                raise
            midpoint = _natural_claim_split_index(batch)
            logger.info(
                "semantic_relation_output_limit_split document_id=%s "
                "batch_ordinal=%d claim_count=%d child_claim_counts=%d,%d",
                batch.document.document_id,
                batch.ordinal,
                len(batch.claims),
                midpoint,
                len(batch.claims) - midpoint,
            )
            children = (
                semantic_relation_sub_batch(batch, batch.claims[:midpoint]),
                semantic_relation_sub_batch(batch, batch.claims[midpoint:]),
            )
            return tuple(
                output
                for child in children
                for output in self._run_batch_with_output_limit_recovery(
                    child,
                    is_cancelled=is_cancelled,
                    on_model_event=on_model_event,
                    retry_scope=retry_scope,
                )
            )

    def _persist(
        self,
        document: SemanticGraphDocument,
        interpretation: SemanticGraphInterpretation,
        *,
        publish_transaction: PublishTransaction | None,
    ) -> bool:
        capability_identity = self._capability_identity()
        prompt_digest = (
            prompt_contract_for(SEMANTIC_RELATION_OPERATION).digest
            if self.gateway is not None
            else None
        )

        def persist(connection: sqlite3.Connection) -> bool:
            result_id = persist_semantic_graph_interpretation_in(
                connection,
                document.document_id,
                interpretation,
                node_count=len(document.candidates),
                capability_identity=capability_identity,
                prompt_contract_digest=prompt_digest,
                candidate_generation_id=document.candidate_generation_id,
                candidate_generation_digest=document.candidate_generation_digest,
            )
            selected = connection.execute(
                "SELECT result_id FROM knowledge_graph_current WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()
            if selected is not None and str(selected[0]) == result_id:
                replace_document_semantic_relations_in(
                    connection,
                    document,
                    interpretation,
                    graph_result_id=result_id,
                )
            return True

        if publish_transaction is not None:
            return publish_transaction(persist)
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    return persist(connection)
            finally:
                connection.close()

    def _input_budget_tokens(self) -> int:
        if self.gateway is None:
            return _DEFAULT_INPUT_BUDGET_TOKENS
        profile_factory = getattr(self.gateway, "execution_profile_for_operation", None)
        if not callable(profile_factory):
            return _DEFAULT_INPUT_BUDGET_TOKENS
        profile = profile_factory(SEMANTIC_RELATION_OPERATION)
        context_room = (
            int(profile.context_capacity)
            - int(profile.prompt_material_tokens)
            - int(profile.provider_output_ceiling_tokens)
        )
        return max(
            _MINIMUM_INPUT_BUDGET_TOKENS,
            min(int(profile.document_input_capacity), context_room),
        )

    def _capability_identity(self) -> str | None:
        if self.gateway is None:
            return None
        profile_factory = getattr(self.gateway, "execution_profile_for_operation", None)
        if not callable(profile_factory):
            return None
        try:
            profile = profile_factory(SEMANTIC_RELATION_OPERATION)
        except (TypeError, ValueError):
            return None
        return str(getattr(profile, "capability_evidence_profile", profile).identity)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _is_cancelled(callback: CancellationCallback | None) -> bool:
    return callback is not None and callback()


def _natural_claim_split_index(batch: SemanticRelationBatch) -> int:
    midpoint = len(batch.claims) / 2
    identity_boundaries = [
        index
        for index in range(1, len(batch.claims))
        if batch.claims[index - 1].candidate_id != batch.claims[index].candidate_id
    ]
    if identity_boundaries:
        return min(identity_boundaries, key=lambda index: (abs(index - midpoint), index))
    return len(batch.claims) // 2


def _report_failure(callback: FailureCallback | None, code: str, reason: str | None = None) -> None:
    if callback is not None:
        callback(code, reason or code.replace("_", " ").capitalize() + ".")
