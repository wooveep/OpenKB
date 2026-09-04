"""Public Corpus Knowledge Synthesis Pipeline and closed publication outcomes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from openkb.desktop_corpus_knowledge import synthesize_qualified_corpus_in
from openkb.desktop_corpus_synthesis_generation import (
    CorpusCandidateInput,
    CorpusGenerationManifest,
    capture_corpus_candidate_inputs_in,
    corpus_generation_manifest_in,
    fail_corpus_manifest_in,
)
from openkb.desktop_corpus_synthesis_tasks import (
    CorpusSynthesisTaskClaim,
    claim_corpus_synthesis_task_in,
    corpus_synthesis_claim_is_active_in,
    finish_corpus_synthesis_task_in,
    mark_corpus_synthesis_qualification_in,
    record_corpus_synthesis_attempt_in,
)
from openkb.desktop_entity_dossier import EntityDossierPlan
from openkb.desktop_entity_dossier_planner import run_entity_dossier_planning
from openkb.desktop_entity_dossier_store import (
    EntityDossierPlanner,
    PlannedEntityDossier,
    PublishedEntityDossier,
    dossier_claims_for_identity_in,
    generation_entity_dossiers_in,
)
from openkb.desktop_import_clock import timestamp
from openkb.desktop_knowledge_generations import (
    complete_prepared_corpus_generation_in,
    current_generation_id_in,
)
from openkb.desktop_model_event import normalize_model_event
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
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
from openkb.desktop_okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    stage_okf_projection_in,
)
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    DesktopValidatedStructuredOutput,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

CorpusSynthesisStatus = Literal["active", "failed", "cancelled", "superseded", "unchanged"]


@dataclass(frozen=True)
class CorpusSynthesisOutcome:
    status: CorpusSynthesisStatus
    generation_id: int | None
    previous_current_generation_id: int | None
    current_generation_id: int | None
    manifest: CorpusGenerationManifest | None
    dossiers: tuple[PublishedEntityDossier, ...] = ()


@dataclass(frozen=True)
class _PreparedCorpusGeneration:
    generation_id: int
    previous_current_generation_id: int | None
    language: Literal["en", "zh"]
    claim: CorpusSynthesisTaskClaim


class CorpusKnowledgeSynthesisPipeline:
    """Coordinate one immutable candidate input set through atomic activation."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._state_dir = desktop_state_dir(self._kb_dir)

    def run_generation(
        self,
        *,
        candidate_generation_ids: tuple[str, ...] = (),
        preferred_language: str | None = None,
        affected_document_ids: tuple[str, ...] = (),
        should_stop: Callable[[], bool] = lambda: False,
        force_generation: bool = False,
        gateway: DesktopModelGateway | None = None,
        retry_scope: str | None = None,
    ) -> CorpusSynthesisOutcome:
        """Run one generation without exposing SQLite or stage ordering to callers."""
        if should_stop():
            return CorpusSynthesisOutcome("cancelled", None, None, None, None)
        if gateway is not None:
            return self._run_model_generation(
                candidate_generation_ids=candidate_generation_ids,
                preferred_language=preferred_language,
                affected_document_ids=affected_document_ids,
                should_stop=should_stop,
                force_generation=force_generation,
                gateway=gateway,
                retry_scope=retry_scope,
            )
        return self._run_deterministic_generation(
            candidate_generation_ids=candidate_generation_ids,
            preferred_language=preferred_language,
            affected_document_ids=affected_document_ids,
            force_generation=force_generation,
        )

    def _run_deterministic_generation(
        self,
        *,
        candidate_generation_ids: tuple[str, ...],
        preferred_language: str | None,
        affected_document_ids: tuple[str, ...],
        force_generation: bool,
    ) -> CorpusSynthesisOutcome:
        staged: Path | None = None
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                previous = current_generation_id_in(connection)
                inputs = capture_corpus_candidate_inputs_in(connection)
                _require_candidate_generations(inputs, candidate_generation_ids)
                before = _latest_generation_id_in(connection)
                current = synthesize_qualified_corpus_in(
                    connection,
                    now=timestamp(),
                    preferred_language=preferred_language,
                    affected_document_ids=affected_document_ids,
                    candidate_inputs=inputs,
                    force_generation=force_generation,
                )
                attempted = _latest_generation_id_in(connection)
                generation_id = attempted if attempted != before else None
                staged = stage_okf_projection_in(connection, self._kb_dir)
                connection.commit()
                outcome = _outcome_in(
                    connection,
                    generation_id=generation_id,
                    previous_current_generation_id=previous,
                    current_generation_id=current,
                )
            except BaseException:
                connection.rollback()
                if staged is not None:
                    discard_okf_projection_staging(staged)
                raise
            finally:
                connection.close()
        assert staged is not None
        try:
            activate_okf_projection(self._kb_dir, staged)
        finally:
            discard_okf_projection_staging(staged)
        return outcome

    def _run_model_generation(
        self,
        *,
        candidate_generation_ids: tuple[str, ...],
        preferred_language: str | None,
        affected_document_ids: tuple[str, ...],
        should_stop: Callable[[], bool],
        force_generation: bool,
        gateway: DesktopModelGateway,
        retry_scope: str | None,
    ) -> CorpusSynthesisOutcome:
        operation = "entity_dossier_planning"
        if not model_operation_dispatch_possible(
            self._kb_dir,
            gateway,
            operation=operation,
            retry_scope=retry_scope,
        ):
            current = self._current_generation_id()
            return CorpusSynthesisOutcome("failed", None, current, current, None)
        prepared_or_outcome = self._prepare_generation(
            candidate_generation_ids=candidate_generation_ids,
            preferred_language=preferred_language,
            affected_document_ids=affected_document_ids,
            force_generation=force_generation,
            gateway=gateway,
            retry_scope=retry_scope,
        )
        if isinstance(prepared_or_outcome, CorpusSynthesisOutcome):
            return prepared_or_outcome
        prepared = prepared_or_outcome
        if should_stop():
            return self._finish_terminal(
                prepared,
                "cancelled",
                error_code="corpus_synthesis_cancelled",
                error_reason="Corpus synthesis was cancelled before model dispatch.",
            )

        def claimed_should_stop() -> bool:
            if should_stop():
                self._finish_terminal(
                    prepared,
                    "cancelled",
                    error_code="corpus_synthesis_cancelled",
                    error_reason="Corpus synthesis was cancelled during model dispatch.",
                )
                return True
            return not self._claim_is_active(prepared.claim)

        outputs: list[DesktopValidatedStructuredOutput[EntityDossierPlan]] = []
        planner = model_entity_dossier_planner(
            gateway,
            should_stop=claimed_should_stop,
            kb_dir=self._kb_dir,
            retry_scope=retry_scope,
            completed_outputs=outputs,
            on_model_event=lambda event: self._record_attempt(prepared.claim, event),
        )
        try:
            planned = self._plan_generation(
                prepared,
                planner,
                should_stop=claimed_should_stop,
            )
        except DesktopModelCancelledError:
            if not self._claim_is_active(prepared.claim):
                return self._outcome_for_prepared(prepared)
            return self._finish_terminal(
                prepared,
                "cancelled",
                error_code="corpus_synthesis_cancelled",
                error_reason="Corpus synthesis was cancelled during model dispatch.",
            )
        except DesktopModelOperationSuspendedError as error:
            return self._finish_terminal(
                prepared,
                "failed",
                error_code="model_operation_suspended",
                error_reason=str(error),
            )
        except DesktopModelCallError as error:
            suspend_analysis_operation_failure(self._kb_dir, gateway, error)
            return self._finish_terminal(
                prepared,
                "failed",
                error_code=error.failure.code,
                error_reason=error.failure.reason,
            )
        except DesktopStructuredOutputInvalidError as error:
            suspend_structured_model_operation(
                self._kb_dir,
                gateway,
                error,
                operation=operation,
                failure_code="entity_dossier_response_invalid",
                reason="Entity Dossier planning returned an invalid result.",
            )
            return self._finish_terminal(
                prepared,
                "failed",
                error_code="entity_dossier_response_invalid",
                error_reason="Entity Dossier planning returned an invalid result.",
            )
        if claimed_should_stop():
            return self._outcome_for_prepared(prepared)
        outcome = self._complete_generation(
            prepared,
            planned,
            should_stop=claimed_should_stop,
        )
        authority = DesktopModelOperationCompletionAuthority.for_retry_scope(retry_scope)
        for output in outputs:
            mark_structured_output_operations_ready(
                self._kb_dir,
                gateway,
                output,
                authority=authority,
            )
        return outcome

    def _prepare_generation(
        self,
        *,
        candidate_generation_ids: tuple[str, ...],
        preferred_language: str | None,
        affected_document_ids: tuple[str, ...],
        force_generation: bool,
        gateway: DesktopModelGateway,
        retry_scope: str | None,
    ) -> _PreparedCorpusGeneration | CorpusSynthesisOutcome:
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                previous = current_generation_id_in(connection)
                inputs = capture_corpus_candidate_inputs_in(connection)
                _require_candidate_generations(inputs, candidate_generation_ids)
                before = _latest_generation_id_in(connection)
                synthesize_qualified_corpus_in(
                    connection,
                    now=timestamp(),
                    preferred_language=preferred_language,
                    affected_document_ids=affected_document_ids,
                    candidate_inputs=inputs,
                    force_generation=force_generation,
                    defer_completion=True,
                )
                attempted = _latest_generation_id_in(connection)
                generation_id = attempted if attempted != before else None
                if generation_id is None:
                    connection.commit()
                    return _outcome_in(
                        connection,
                        generation_id=None,
                        previous_current_generation_id=previous,
                        current_generation_id=current_generation_id_in(connection),
                    )
                language = _generation_language_in(
                    connection,
                    generation_id,
                    preferred_language=preferred_language,
                )
                claim = claim_corpus_synthesis_task_in(
                    connection,
                    generation_id,
                    provider=gateway.provider_name,
                    model=gateway.model_name,
                    retry_scope=retry_scope,
                    now=timestamp(),
                )
                connection.commit()
                return _PreparedCorpusGeneration(
                    generation_id,
                    previous,
                    language,
                    claim,
                )
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _plan_generation(
        self,
        prepared: _PreparedCorpusGeneration,
        planner: EntityDossierPlanner,
        *,
        should_stop: Callable[[], bool],
    ) -> dict[str, PlannedEntityDossier]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT identity_id, title FROM knowledge_generation_items "
                "WHERE generation_id = ? AND kind = 'entity' ORDER BY item_key",
                (prepared.generation_id,),
            ).fetchall()
            known_identity_ids = frozenset(
                str(row[0])
                for row in connection.execute(
                    "SELECT identity_id FROM knowledge_generation_items "
                    "WHERE generation_id = ? AND identity_id IS NOT NULL",
                    (prepared.generation_id,),
                )
            )
            planned: dict[str, PlannedEntityDossier] = {}
            for row in rows:
                if should_stop():
                    raise DesktopModelCancelledError()
                identity_id = str(row[0])
                planned[identity_id] = planner(
                    document_name=str(row[1]),
                    generation_id=prepared.generation_id,
                    identity_id=identity_id,
                    claims=dossier_claims_for_identity_in(
                        connection, prepared.generation_id, identity_id
                    ),
                    language=prepared.language,
                    known_related_identity_ids=known_identity_ids - {identity_id},
                )
            return planned
        finally:
            connection.close()

    def _complete_generation(
        self,
        prepared: _PreparedCorpusGeneration,
        planned: dict[str, PlannedEntityDossier],
        *,
        should_stop: Callable[[], bool],
    ) -> CorpusSynthesisOutcome:
        staged: Path | None = None

        def prepared_planner(**kwargs) -> PlannedEntityDossier:
            identity_id = str(kwargs["identity_id"])
            if identity_id not in planned:
                raise KeyError(identity_id)
            return planned[identity_id]

        if should_stop():
            return self._outcome_for_prepared(prepared)
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not corpus_synthesis_claim_is_active_in(connection, prepared.claim):
                    connection.rollback()
                    return self._outcome_for_prepared(prepared)
                now = timestamp()
                if not mark_corpus_synthesis_qualification_in(
                    connection,
                    prepared.claim,
                    now=now,
                ):
                    connection.rollback()
                    return self._outcome_for_prepared(prepared)
                current = complete_prepared_corpus_generation_in(
                    connection,
                    prepared.generation_id,
                    language=prepared.language,
                    now=now,
                    planner=prepared_planner,
                )
                outcome = _outcome_in(
                    connection,
                    generation_id=prepared.generation_id,
                    previous_current_generation_id=prepared.previous_current_generation_id,
                    current_generation_id=current,
                )
                if outcome.status == "active":
                    staged = stage_okf_projection_in(connection, self._kb_dir)
                task_status: Literal["completed", "failed", "superseded"]
                task_status = (
                    "completed"
                    if outcome.status == "active"
                    else "superseded"
                    if outcome.status == "superseded"
                    else "failed"
                )
                task_error_code = (
                    None
                    if task_status == "completed"
                    else "candidate_generation_superseded"
                    if task_status == "superseded"
                    else "corpus_qualification_failed"
                )
                task_error_reason = (
                    None
                    if task_status == "completed"
                    else "A newer Candidate Registry generation superseded this synthesis."
                    if task_status == "superseded"
                    else "Corpus synthesis did not pass qualification."
                )
                if not finish_corpus_synthesis_task_in(
                    connection,
                    prepared.claim,
                    status=task_status,
                    error_code=task_error_code,
                    error_reason=task_error_reason,
                    now=now,
                ):
                    raise RuntimeError("Corpus synthesis claim was lost before publication.")
                connection.commit()
            except BaseException:
                connection.rollback()
                if staged is not None:
                    discard_okf_projection_staging(staged)
                raise
            finally:
                connection.close()
        if staged is not None:
            try:
                activate_okf_projection(self._kb_dir, staged)
            finally:
                discard_okf_projection_staging(staged)
        return outcome

    def _finish_terminal(
        self,
        prepared: _PreparedCorpusGeneration,
        lifecycle_state: Literal["failed", "cancelled"],
        *,
        error_code: str,
        error_reason: str,
    ) -> CorpusSynthesisOutcome:
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        "UPDATE knowledge_generations SET qualification_state = 'failed' "
                        "WHERE generation_id = ?",
                        (prepared.generation_id,),
                    )
                    fail_corpus_manifest_in(
                        connection,
                        prepared.generation_id,
                        now=timestamp(),
                        lifecycle_state=lifecycle_state,
                    )
                    finish_corpus_synthesis_task_in(
                        connection,
                        prepared.claim,
                        status=lifecycle_state,
                        error_code=error_code,
                        error_reason=error_reason,
                        now=timestamp(),
                    )
                return _outcome_in(
                    connection,
                    generation_id=prepared.generation_id,
                    previous_current_generation_id=prepared.previous_current_generation_id,
                    current_generation_id=current_generation_id_in(connection),
                )
            finally:
                connection.close()

    def _claim_is_active(self, claim: CorpusSynthesisTaskClaim) -> bool:
        try:
            connection = self._connect()
            try:
                return corpus_synthesis_claim_is_active_in(connection, claim)
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return False

    def _record_attempt(self, claim: CorpusSynthesisTaskClaim, event: object) -> None:
        lifecycle = normalize_model_event(event)
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    record_corpus_synthesis_attempt_in(
                        connection,
                        claim,
                        attempt=lifecycle.attempt,
                        error_code=lifecycle.error_code,
                        error_reason=lifecycle.reason,
                        now=timestamp(),
                    )
            finally:
                connection.close()

    def _outcome_for_prepared(
        self,
        prepared: _PreparedCorpusGeneration,
    ) -> CorpusSynthesisOutcome:
        connection = self._connect()
        try:
            return _outcome_in(
                connection,
                generation_id=prepared.generation_id,
                previous_current_generation_id=prepared.previous_current_generation_id,
                current_generation_id=current_generation_id_in(connection),
            )
        finally:
            connection.close()

    def _current_generation_id(self) -> int | None:
        connection = self._connect()
        try:
            return current_generation_id_in(connection)
        finally:
            connection.close()

    def generation(self, generation_id: int) -> CorpusSynthesisOutcome:
        connection = self._connect()
        try:
            current = current_generation_id_in(connection)
            return _outcome_in(
                connection,
                generation_id=generation_id,
                previous_current_generation_id=None,
                current_generation_id=current,
            )
        finally:
            connection.close()

    def request_cancel(self, generation_id: int) -> bool:
        """Invalidate a running durable claim before its provider is asked to stop."""
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT execution_token, retry_scope "
                    "FROM knowledge_corpus_synthesis_tasks "
                    "WHERE generation_id = ? AND status = 'running'",
                    (generation_id,),
                ).fetchone()
                if row is None or row[0] is None:
                    connection.rollback()
                    return False
                claim = CorpusSynthesisTaskClaim(
                    generation_id,
                    str(row[0]),
                    str(row[1]) if row[1] is not None else None,
                )
                now = timestamp()
                connection.execute(
                    "UPDATE knowledge_generations SET qualification_state = 'failed' "
                    "WHERE generation_id = ?",
                    (generation_id,),
                )
                fail_corpus_manifest_in(
                    connection,
                    generation_id,
                    now=now,
                    lifecycle_state="cancelled",
                )
                cancelled = finish_corpus_synthesis_task_in(
                    connection,
                    claim,
                    status="cancelled",
                    error_code="corpus_synthesis_cancelled",
                    error_reason="Corpus synthesis was cancelled by its owner.",
                    now=now,
                )
                connection.commit()
                return cancelled
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


DesktopCorpusKnowledgeSynthesisPipeline = CorpusKnowledgeSynthesisPipeline


def model_entity_dossier_planner(
    gateway: DesktopModelGateway,
    *,
    should_stop: Callable[[], bool],
    kb_dir: Path | None = None,
    retry_scope: str | None = None,
    completed_outputs: list[DesktopValidatedStructuredOutput[EntityDossierPlan]] | None = None,
    on_model_event: Callable[[object], None] | None = None,
) -> EntityDossierPlanner:
    contract_digest = prompt_contract_for("entity_dossier_planning").digest

    def plan(**kwargs) -> PlannedEntityDossier:
        def invoke(request):
            if kb_dir is not None:
                require_model_operation_dispatch(
                    kb_dir,
                    gateway,
                    request,
                    retry_scope=retry_scope,
                )
            return gateway.analyze(
                request,
                on_event=on_model_event or (lambda _event: None),
                is_cancelled=should_stop,
            )

        run = run_entity_dossier_planning(
            **kwargs,
            invoke=invoke,
        )
        if completed_outputs is not None:
            completed_outputs.append(run.output)
        provenance = json.dumps(
            {
                "provider": gateway.provider_name,
                "model": gateway.model_name,
                "call_id": run.result.call_id,
                "response_sha256": hashlib.sha256(run.result.content.encode("utf-8")).hexdigest(),
                "repaired": run.repaired,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return PlannedEntityDossier(
            plan=run.plan,
            planning_operation="entity_dossier_planning",
            prompt_contract_digest=contract_digest,
            planner_provenance_json=provenance,
        )

    return plan


def _outcome_in(
    connection: sqlite3.Connection,
    *,
    generation_id: int | None,
    previous_current_generation_id: int | None,
    current_generation_id: int | None,
) -> CorpusSynthesisOutcome:
    if generation_id is None:
        return CorpusSynthesisOutcome(
            "unchanged",
            None,
            previous_current_generation_id,
            current_generation_id,
            None,
        )
    manifest = corpus_generation_manifest_in(connection, generation_id)
    lifecycle = manifest.lifecycle_state if manifest is not None else "failed"
    status: CorpusSynthesisStatus = (
        "active"
        if current_generation_id == generation_id and lifecycle == "active"
        else "cancelled"
        if lifecycle == "cancelled"
        else "superseded"
        if lifecycle == "superseded"
        else "failed"
    )
    return CorpusSynthesisOutcome(
        status=status,
        generation_id=generation_id,
        previous_current_generation_id=previous_current_generation_id,
        current_generation_id=current_generation_id,
        manifest=manifest,
        dossiers=generation_entity_dossiers_in(connection, generation_id),
    )


def _require_candidate_generations(
    inputs: tuple[CorpusCandidateInput, ...],
    requested: tuple[str, ...],
) -> None:
    if not requested:
        return
    selected = tuple(item.candidate_generation_id for item in inputs)
    if tuple(sorted(dict.fromkeys(requested))) != tuple(sorted(selected)):
        raise ValueError("Requested Candidate Registry Generations are not current and complete.")


def _latest_generation_id_in(connection: sqlite3.Connection) -> int | None:
    row = connection.execute("SELECT MAX(generation_id) FROM knowledge_generations").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def _generation_language_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    preferred_language: str | None,
) -> Literal["en", "zh"]:
    if preferred_language in {"en", "zh"}:
        return cast(Literal["en", "zh"], preferred_language)
    texts = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT claim_text FROM knowledge_generation_item_sources "
            "WHERE generation_id = ? ORDER BY item_key, source_id",
            (generation_id,),
        )
        if str(row[0]).strip()
    )
    chinese = sum(any("\u3400" <= character <= "\u9fff" for character in text) for text in texts)
    return "zh" if chinese * 2 > max(1, len(texts)) else "en"
