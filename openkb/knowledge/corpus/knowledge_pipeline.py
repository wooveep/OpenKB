"""Public Corpus Knowledge Synthesis Pipeline and closed publication outcomes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from openkb.importing.clock import timestamp
from openkb.knowledge.corpus.knowledge import synthesize_qualified_corpus_in
from openkb.knowledge.corpus.model_review import review_pending_claims
from openkb.knowledge.corpus.synthesis_generation import (
    CorpusCandidateInput,
    CorpusGenerationManifest,
    bind_generation_graph_inputs_in,
    capture_corpus_candidate_inputs_in,
    corpus_generation_manifest_in,
    fail_corpus_manifest_in,
)
from openkb.knowledge.corpus.synthesis_tasks import (
    CorpusSynthesisTaskClaim,
    claim_corpus_synthesis_task_in,
    corpus_synthesis_claim_is_active_in,
    finish_corpus_synthesis_task_in,
    mark_corpus_synthesis_qualification_in,
    record_corpus_synthesis_attempt_in,
)
from openkb.knowledge.pages.generations import (
    complete_prepared_corpus_generation_in,
    current_generation_id_in,
)
from openkb.knowledge.pages.model_planner import model_knowledge_page_planner
from openkb.knowledge.pages.okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    stage_okf_projection_in,
)
from openkb.knowledge.pages.page import knowledge_page_claim_snapshot_digest
from openkb.knowledge.pages.planning import KnowledgePagePlan
from openkb.knowledge.pages.relationships import rebuild_generation_relationships_in
from openkb.knowledge.pages.store import (
    DeferredKnowledgePage,
    KnowledgePagePlanner,
    PlannedKnowledgePage,
    PublishedKnowledgePage,
    generation_knowledge_pages_in,
    knowledge_page_claims_for_identity_in,
    knowledge_page_relations_for_identity_in,
)
from openkb.locks import kb_ingest_lock
from openkb.models.event import normalize_model_event
from openkb.models.gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
)
from openkb.models.result_failure import (
    DesktopModelOperationCompletionAuthority,
    DesktopModelOperationSuspendedError,
    mark_structured_output_operations_ready,
    model_operation_dispatch_possible,
    suspend_analysis_operation_failure,
)
from openkb.models.structured_output import (
    DesktopStructuredOutputInvalidError,
    DesktopValidatedStructuredOutput,
)
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

CorpusSynthesisStatus = Literal["active", "failed", "cancelled", "superseded", "unchanged"]


@dataclass(frozen=True)
class CorpusSynthesisOutcome:
    status: CorpusSynthesisStatus
    generation_id: int | None
    previous_current_generation_id: int | None
    current_generation_id: int | None
    manifest: CorpusGenerationManifest | None
    pages: tuple[PublishedKnowledgePage, ...] = ()


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
        can_dispatch: Callable[[], bool] = lambda: True,
    ) -> CorpusSynthesisOutcome:
        """Run one generation without exposing SQLite or stage ordering to callers."""
        if should_stop():
            return CorpusSynthesisOutcome("cancelled", None, None, None, None)
        if gateway is None:
            return self._run_without_planner(
                candidate_generation_ids=candidate_generation_ids,
                preferred_language=preferred_language,
                affected_document_ids=affected_document_ids,
                should_stop=should_stop,
                force_generation=force_generation,
            )
        return self._run_model_generation(
            candidate_generation_ids=candidate_generation_ids,
            preferred_language=preferred_language,
            affected_document_ids=affected_document_ids,
            should_stop=should_stop,
            force_generation=force_generation,
            gateway=gateway,
            retry_scope=retry_scope,
            can_dispatch=can_dispatch,
        )

    def _run_without_planner(
        self,
        *,
        candidate_generation_ids: tuple[str, ...],
        preferred_language: str | None,
        affected_document_ids: tuple[str, ...],
        should_stop: Callable[[], bool],
        force_generation: bool,
    ) -> CorpusSynthesisOutcome:
        prepared_or_outcome = self._prepare_generation(
            candidate_generation_ids=candidate_generation_ids,
            preferred_language=preferred_language,
            affected_document_ids=affected_document_ids,
            force_generation=force_generation,
            provider="unavailable",
            model="unavailable",
            retry_scope=None,
        )
        if isinstance(prepared_or_outcome, CorpusSynthesisOutcome):
            return prepared_or_outcome
        prepared = prepared_or_outcome
        if should_stop():
            return self._finish_terminal(
                prepared,
                "cancelled",
                error_code="corpus_synthesis_cancelled",
                error_reason="Corpus synthesis was cancelled before page planning.",
            )
        outcomes = self._deferred_generation(
            prepared,
            error_code="knowledge_page_planner_unavailable",
        )
        return self._complete_generation(prepared, outcomes, should_stop=should_stop)

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
        can_dispatch: Callable[[], bool],
    ) -> CorpusSynthesisOutcome:
        operation = "knowledge_page_planning"
        try:
            review_pending_claims(self._kb_dir, gateway, should_stop, retry_scope, can_dispatch)
        except DesktopModelCancelledError:
            return CorpusSynthesisOutcome("cancelled", None, None, None, None)
        if not model_operation_dispatch_possible(
            self._kb_dir,
            gateway,
            operation=operation,
            retry_scope=retry_scope,
        ):
            return self._run_without_planner(
                candidate_generation_ids=candidate_generation_ids,
                preferred_language=preferred_language,
                affected_document_ids=affected_document_ids,
                should_stop=should_stop,
                force_generation=force_generation,
            )
        prepared_or_outcome = self._prepare_generation(
            candidate_generation_ids=candidate_generation_ids,
            preferred_language=preferred_language,
            affected_document_ids=affected_document_ids,
            force_generation=force_generation,
            provider=gateway.provider_name,
            model=gateway.model_name,
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

        outputs: list[DesktopValidatedStructuredOutput[KnowledgePagePlan]] = []
        planner = model_knowledge_page_planner(
            gateway,
            should_stop=claimed_should_stop,
            kb_dir=self._kb_dir,
            retry_scope=retry_scope,
            completed_outputs=outputs,
            on_model_event=lambda event: self._record_attempt(prepared.claim, event),
            can_dispatch=can_dispatch,
        )
        try:
            planned = self._plan_generation(
                prepared,
                planner,
                gateway=gateway,
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
        provider: str,
        model: str,
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
                bind_generation_graph_inputs_in(connection, generation_id, now=timestamp())
                rebuild_generation_relationships_in(connection, generation_id)
                language = _generation_language_in(
                    connection,
                    generation_id,
                    preferred_language=preferred_language,
                )
                claim = claim_corpus_synthesis_task_in(
                    connection,
                    generation_id,
                    provider=provider,
                    model=model,
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
        planner: KnowledgePagePlanner,
        *,
        gateway: DesktopModelGateway,
        should_stop: Callable[[], bool],
    ) -> dict[str, PlannedKnowledgePage | DeferredKnowledgePage]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT identity_id, title FROM knowledge_generation_items "
                "WHERE generation_id = ? AND identity_id IS NOT NULL ORDER BY item_key",
                (prepared.generation_id,),
            ).fetchall()
            planned: dict[str, PlannedKnowledgePage | DeferredKnowledgePage] = {}
            for row in rows:
                if should_stop():
                    raise DesktopModelCancelledError()
                identity_id = str(row[0])
                claims = knowledge_page_claims_for_identity_in(
                    connection, prepared.generation_id, identity_id
                )
                snapshot_digest = knowledge_page_claim_snapshot_digest(claims)
                if not claims:
                    planned[identity_id] = DeferredKnowledgePage(
                        identity_id,
                        snapshot_digest,
                        ("empty_claim_snapshot",),
                    )
                    continue
                try:
                    planned[identity_id] = planner(
                        document_name=str(row[1]),
                        generation_id=prepared.generation_id,
                        identity_id=identity_id,
                        title=str(row[1]),
                        claims=claims,
                        relations=knowledge_page_relations_for_identity_in(
                            connection,
                            prepared.generation_id,
                            identity_id,
                        ),
                        knowledge_language=prepared.language,
                    )
                except DesktopStructuredOutputInvalidError:
                    planned[identity_id] = DeferredKnowledgePage(
                        identity_id,
                        snapshot_digest,
                        ("knowledge_page_plan_invalid",),
                    )
                except DesktopModelOperationSuspendedError:
                    planned[identity_id] = DeferredKnowledgePage(
                        identity_id,
                        snapshot_digest,
                        ("knowledge_page_planner_suspended",),
                    )
                except DesktopModelCallError as error:
                    suspend_analysis_operation_failure(self._kb_dir, gateway, error)
                    planned[identity_id] = DeferredKnowledgePage(
                        identity_id,
                        snapshot_digest,
                        (error.failure.code,),
                    )
            return planned
        finally:
            connection.close()

    def _deferred_generation(
        self,
        prepared: _PreparedCorpusGeneration,
        *,
        error_code: str,
    ) -> dict[str, PlannedKnowledgePage | DeferredKnowledgePage]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT DISTINCT identity_id FROM knowledge_generation_items "
                "WHERE generation_id = ? AND identity_id IS NOT NULL ORDER BY identity_id",
                (prepared.generation_id,),
            ).fetchall()
            result: dict[str, PlannedKnowledgePage | DeferredKnowledgePage] = {}
            for row in rows:
                identity_id = str(row[0])
                claims = knowledge_page_claims_for_identity_in(
                    connection,
                    prepared.generation_id,
                    identity_id,
                )
                result[identity_id] = DeferredKnowledgePage(
                    identity_id,
                    knowledge_page_claim_snapshot_digest(claims),
                    (error_code,),
                )
            return result
        finally:
            connection.close()

    def _complete_generation(
        self,
        prepared: _PreparedCorpusGeneration,
        planned: dict[str, PlannedKnowledgePage | DeferredKnowledgePage],
        *,
        should_stop: Callable[[], bool],
    ) -> CorpusSynthesisOutcome:
        staged: Path | None = None

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
                    page_outcomes=planned,
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
        connection = connect_database(self._database_path)
        return connection


DesktopCorpusKnowledgeSynthesisPipeline = CorpusKnowledgeSynthesisPipeline


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
        pages=generation_knowledge_pages_in(connection, generation_id),
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
