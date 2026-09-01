"""Grounded-answer orchestration over vectorless Desktop Evidence Packs."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import cast

from openkb.desktop_answer_store import DesktopGroundedAnswerStore, new_answer
from openkb.desktop_answer_types import (
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopGroundedAnswer,
    DesktopKnowledgeGuidance,
)
from openkb.desktop_model_execution_profile import estimate_model_tokens
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_answer_capability_verified,
)
from openkb.desktop_model_result_failure import (
    authorize_model_operation_retry_group,
    revoke_model_operation_retry_scope,
)
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_trace import (
    DesktopAnswerCoverageTrace,
    DesktopRetrievalTrace,
)
from openkb.desktop_structured_output import structured_output_repair_contract_digest

AnswerDeltaCallback = Callable[[str, str, bool, int], None]
AnswerCancellationCallback = Callable[[], bool]
AnswerModelEventCallback = Callable[[object], None]
_DEFAULT_ANSWER_CONTEXT_CAPACITY_TOKENS = 4_096
_EVIDENCE_GROUNDING_SHARE = 0.75
_STREAM_CHUNK_CHARS = 96


@dataclass(frozen=True)
class DesktopGroundedAnswerGeneration:
    """One non-persistent run of the same answer generation used by Desktop."""

    answer_text: str | None
    degradations: tuple[str, ...]
    model_calls: int = 0
    model_input_characters: int = 0
    model_output_characters: int = 0
    interruption_code: str | None = None
    interruption_reason: str | None = None


class DesktopGroundedAnswerService:
    """Answer from cited Available Knowledge and retain incomplete model attempts."""

    def __init__(self, kb_dir: Path, *, model_gateway: DesktopModelGateway | None = None) -> None:
        interactive_gateway = _interactive_gateway(model_gateway)
        self._kb_dir = kb_dir.expanduser().resolve()
        self._retriever = DesktopEvidenceRetriever(
            kb_dir,
            model_gateway=interactive_gateway,
        )
        self._store = DesktopGroundedAnswerStore(kb_dir)
        self._model_gateway = interactive_gateway

    def answer(
        self,
        question: str,
        *,
        on_delta: AnswerDeltaCallback | None = None,
        is_cancelled: AnswerCancellationCallback | None = None,
        on_model_event: AnswerModelEventCallback | None = None,
    ) -> DesktopGroundedAnswer:
        """Stream and persist a completed answer or the text reached before interruption."""
        answer = self._attempt(
            question,
            answer_id=uuid.uuid4().hex,
            on_delta=on_delta,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
            conversation_context=(),
        )
        return self._store.save(answer)

    def retry(
        self,
        answer_id: str,
        *,
        on_delta: AnswerDeltaCallback | None = None,
        is_cancelled: AnswerCancellationCallback | None = None,
        on_model_event: AnswerModelEventCallback | None = None,
    ) -> DesktopGroundedAnswer:
        """Retry from an interrupted card without replacing it until a full answer succeeds."""
        interrupted = self._store.interrupted(answer_id)
        replacement = self._attempt(
            interrupted.question,
            answer_id=interrupted.answer_id,
            created_at=interrupted.created_at,
            on_delta=on_delta,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
            conversation_context=(),
            retry_suspended_operations=True,
        )
        if replacement.status == "interrupted":
            return interrupted
        return self._store.replace_interrupted(replacement)

    def generate(
        self,
        question: str,
        *,
        conversation_context: tuple[tuple[str, str], ...] = (),
        on_delta: AnswerDeltaCallback | None = None,
        is_cancelled: AnswerCancellationCallback | None = None,
        on_model_event: AnswerModelEventCallback | None = None,
        retry_suspended_operations: bool = False,
    ) -> DesktopGroundedAnswer:
        """Generate an auditable answer without writing the legacy flat-answer tables."""
        return self._attempt(
            question,
            answer_id=uuid.uuid4().hex,
            on_delta=on_delta,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
            conversation_context=conversation_context,
            retry_suspended_operations=retry_suspended_operations,
        )

    def _attempt(
        self,
        question: str,
        *,
        answer_id: str,
        created_at: str | None = None,
        on_delta: AnswerDeltaCallback | None,
        is_cancelled: AnswerCancellationCallback | None,
        on_model_event: AnswerModelEventCallback | None,
        conversation_context: tuple[tuple[str, str], ...],
        retry_suspended_operations: bool = False,
    ) -> DesktopGroundedAnswer:
        operation_retry_scopes: dict[str, str] = {}
        retry_scope: str | None = None
        if retry_suspended_operations and self._model_gateway is not None:
            retry_scope = f"grounded_answer:{answer_id}:{uuid.uuid4().hex}"
            authorize_model_operation_retry_group(
                self._kb_dir,
                self._model_gateway,
                retry_scope=retry_scope,
                contracts=tuple(
                    contract
                    for operation in (
                        "retrieval_plan",
                        "page_tree_selection",
                        "knowledge_navigation_step",
                    )
                    for contract in (
                        (operation, prompt_contract_for(operation).digest),
                        (
                            "structured_output_repair",
                            structured_output_repair_contract_digest(operation),
                        ),
                    )
                ),
            )
            operation_retry_scopes = {
                "retrieval_plan": retry_scope,
                "page_tree_selection": retry_scope,
                "knowledge_navigation_step": retry_scope,
            }
        try:
            pack = prepare_grounded_evidence_pack(
                self._retriever.retrieve(
                    question,
                    is_cancelled=is_cancelled,
                    on_model_event=on_model_event,
                    operation_retry_scopes=operation_retry_scopes,
                ),
                context_capacity_tokens=_answer_context_capacity(self._model_gateway),
            )
        finally:
            if retry_scope is not None:
                revoke_model_operation_retry_scope(self._kb_dir, retry_scope)
        emitted = False
        visible_attempt = 0
        replace_pending = False
        visible_answer_text = ""
        stream_state_lock = Lock()

        def emit(delta: str, attempt: int, *, replace: bool = False) -> None:
            nonlocal emitted, replace_pending, visible_answer_text, visible_attempt
            with stream_state_lock:
                if attempt < visible_attempt:
                    return
                if attempt > visible_attempt:
                    visible_attempt = attempt
                    replace = True
                    replace_pending = False
                elif replace_pending:
                    replace = True
                    replace_pending = False
                if replace:
                    emitted = False
                    visible_answer_text = ""
                if delta:
                    emitted = True
                    visible_answer_text += delta
            if on_delta is not None:
                on_delta(answer_id, delta, replace, attempt)

        def reset(attempt: int) -> None:
            nonlocal emitted, replace_pending, visible_attempt
            with stream_state_lock:
                if attempt <= visible_attempt:
                    return
                visible_attempt = attempt
                emitted = False
                replace_pending = True

        def interrupted_answer(
            interruption_code: str | None, interruption_reason: str | None
        ) -> DesktopGroundedAnswer:
            with stream_state_lock:
                partial_text = visible_answer_text
            return new_answer(
                answer_id=answer_id,
                question=pack.retrieval_plan.query,
                answer_text=partial_text,
                retrieval_plan=pack.retrieval_plan,
                citations=pack.evidence,
                degradations=pack.degradations,
                source_images=pack.source_images,
                retrieval_trace=pack.retrieval_trace,
                status="interrupted",
                interruption_code=interruption_code,
                interruption_reason=interruption_reason,
                created_at=created_at,
            )

        generation = generate_grounded_answer(
            question,
            pack,
            model_gateway=self._model_gateway,
            on_delta=lambda attempt, delta: emit(delta, attempt),
            on_reset=reset,
            is_cancelled=is_cancelled,
            conversation_context=conversation_context,
            on_model_event=on_model_event,
        )
        if generation.answer_text is None:
            return interrupted_answer(
                generation.interruption_code,
                generation.interruption_reason,
            )
        with stream_state_lock:
            final_attempt = max(visible_attempt, 1)
            needs_fallback_stream = not emitted or bool(generation.degradations)
            if generation.degradations and visible_attempt:
                final_attempt = visible_attempt + 1
        if needs_fallback_stream:
            for ordinal, chunk in enumerate(_stream_chunks(generation.answer_text)):
                if _is_cancelled(is_cancelled):
                    return interrupted_answer("answer_cancelled", "Answer generation was stopped.")
                emit(
                    chunk,
                    final_attempt,
                    replace=bool(generation.degradations and ordinal == 0),
                )
                if _is_cancelled(is_cancelled):
                    return interrupted_answer("answer_cancelled", "Answer generation was stopped.")
        if _is_cancelled(is_cancelled):
            return interrupted_answer("answer_cancelled", "Answer generation was stopped.")
        answer = new_answer(
            answer_id=answer_id,
            question=pack.retrieval_plan.query,
            answer_text=generation.answer_text,
            retrieval_plan=pack.retrieval_plan,
            citations=pack.evidence,
            degradations=tuple((*pack.degradations, *generation.degradations)),
            source_images=pack.source_images,
            retrieval_trace=pack.retrieval_trace,
            created_at=created_at,
        )
        return answer

    def list(self) -> tuple[DesktopGroundedAnswer, ...]:
        return self._store.list()


def _interactive_gateway(
    gateway: DesktopModelGateway | None,
) -> DesktopModelGateway | None:
    if gateway is None:
        return None
    select_lane = getattr(gateway, "for_lane", None)
    if not callable(select_lane):
        return gateway
    return cast(DesktopModelGateway, select_lane("interactive"))


def prepare_grounded_evidence_pack(
    pack: DesktopEvidencePack,
    *,
    context_capacity_tokens: int | None = None,
) -> DesktopEvidencePack:
    """Apply the model-aware Evidence/Guidance budget before generation and scoring."""
    capacity = context_capacity_tokens or _DEFAULT_ANSWER_CONTEXT_CAPACITY_TOKENS
    policy = prompt_contract_for("grounded_answer").token_budget_policy
    reserve_value = policy.get("reserve_output_tokens")
    share_value = policy.get("document_input_share")
    reserve = reserve_value if isinstance(reserve_value, int) else 2_048
    document_share = float(share_value) if isinstance(share_value, (int, float)) else 0.7
    grounding_budget = max(0, int(max(0, capacity - reserve) * document_share))
    evidence_budget = int(grounding_budget * _EVIDENCE_GROUNDING_SHARE)
    guidance_budget = grounding_budget - evidence_budget
    sent_evidence, evidence_tokens = _evidence_for_prompt(pack.evidence, evidence_budget)
    sent_evidence_ids = {reference.evidence_id for reference in sent_evidence}
    evidence_bound_guidance = tuple(
        item
        for item in pack.guidance
        if item.source_evidence_ids and set(item.source_evidence_ids) <= sent_evidence_ids
    )
    sent_guidance, guidance_tokens = _guidance_for_prompt(evidence_bound_guidance, guidance_budget)
    coverage_aspects = _coverage_after_grounding_budget(
        pack.retrieval_trace.coverage_aspects,
        sent_evidence_ids,
    )
    coverage_gate_state = (
        _coverage_state(coverage_aspects)
        if coverage_aspects
        else pack.retrieval_trace.coverage_gate_state
    )
    if not coverage_aspects and pack.guidance and len(sent_guidance) < len(pack.guidance):
        coverage_gate_state = "partial" if sent_guidance else "uncovered"
    trace = replace(
        pack.retrieval_trace.with_canonical_evidence_ids(
            tuple(reference.evidence_id for reference in sent_evidence)
        ),
        navigation_routes=tuple(item.route for item in sent_guidance),
        grounding_input_budget_tokens=grounding_budget,
        evidence_input_tokens=evidence_tokens,
        guidance_input_tokens=guidance_tokens,
        coverage_gate_state=coverage_gate_state,
        coverage_aspects=coverage_aspects,
    )
    return DesktopEvidencePack(
        retrieval_plan=pack.retrieval_plan,
        evidence=sent_evidence,
        degradations=pack.degradations,
        source_images=tuple(
            image for image in pack.source_images if image.evidence_id in sent_evidence_ids
        ),
        retrieval_trace=trace,
        retrieval_model_cost=pack.retrieval_model_cost,
        guidance=sent_guidance,
    )


def generate_grounded_answer(
    question: str,
    pack: DesktopEvidencePack,
    *,
    model_gateway: DesktopModelGateway | None,
    on_delta: Callable[[int, str], None] | None = None,
    on_reset: Callable[[int], None] | None = None,
    is_cancelled: AnswerCancellationCallback | None = None,
    conversation_context: tuple[tuple[str, str], ...] = (),
    on_model_event: AnswerModelEventCallback | None = None,
) -> DesktopGroundedAnswerGeneration:
    """Generate from an already-retrieved pack without saving an answer record.

    The workbench and retrieval evaluator share this seam, so the latter
    measures the exact grounded-answer prompt and Model Gateway policy users
    receive rather than a synthetic evidence concatenation.
    """
    if _is_cancelled(is_cancelled):
        return _cancelled_generation()
    if model_gateway is None:
        return DesktopGroundedAnswerGeneration(
            _deterministic_answer(question, pack.evidence), ("answer_model_unavailable",)
        )
    if not gateway_answer_capability_verified(model_gateway):
        return DesktopGroundedAnswerGeneration(
            _deterministic_answer(question, pack.evidence), ("answer_model_unverified",)
        )

    prompt = _answer_prompt(
        question,
        pack.evidence,
        pack.guidance,
        conversation_context,
        pack.retrieval_trace,
    )
    attempts = 0

    def observe(event) -> None:
        nonlocal attempts
        if event.status in {
            "connecting",
            "awaiting_model_result",
            "model_output_activity",
            "validating",
        }:
            attempts = max(attempts, event.attempt)
        if on_model_event is not None:
            on_model_event(event)

    try:
        result = model_gateway.stream(
            DesktopModelRequest("grounded_answer", "Grounded answer", prompt),
            on_event=observe,
            on_delta=on_delta or (lambda _attempt, _delta: None),
            on_reset=on_reset,
            is_cancelled=is_cancelled,
        )
        attempts = max(attempts, result.attempt_count)
        answer_text = result.content.strip()
        if _is_cancelled(is_cancelled):
            return _cancelled_generation(model_calls=attempts, prompt=prompt)
        if answer_text:
            return DesktopGroundedAnswerGeneration(
                answer_text,
                (),
                model_calls=attempts,
                model_input_characters=len(prompt) * attempts,
                model_output_characters=len(result.content),
            )
        return DesktopGroundedAnswerGeneration(
            _deterministic_answer(question, pack.evidence),
            ("answer_model_fallback",),
            model_calls=attempts,
            model_input_characters=len(prompt) * attempts,
            model_output_characters=len(result.content),
        )
    except DesktopModelCancelledError:
        return _cancelled_generation(model_calls=attempts, prompt=prompt)
    except DesktopModelCallError as error:
        return DesktopGroundedAnswerGeneration(
            None,
            (),
            model_calls=attempts,
            model_input_characters=len(prompt) * attempts,
            interruption_code=error.failure.code,
            interruption_reason=error.failure.reason,
        )


def _evidence_for_prompt(
    evidence: tuple[DesktopEvidenceRef, ...],
    budget_tokens: int,
) -> tuple[tuple[DesktopEvidenceRef, ...], int]:
    included: list[DesktopEvidenceRef] = []
    used = 0
    for ordinal, reference in enumerate(evidence, start=1):
        item = f"[{ordinal}] {reference.document_name} — {reference.section}\n{reference.excerpt}\n"
        item_tokens = estimate_model_tokens(item)
        if used + item_tokens > budget_tokens:
            break
        included.append(reference)
        used += item_tokens
    return tuple(included), used


def _guidance_for_prompt(
    guidance: tuple[DesktopKnowledgeGuidance, ...],
    budget_tokens: int,
) -> tuple[tuple[DesktopKnowledgeGuidance, ...], int]:
    included: list[DesktopKnowledgeGuidance] = []
    used = 0
    for item in guidance:
        rendered = _guidance_text(item)
        item_tokens = estimate_model_tokens(rendered)
        if used + item_tokens > budget_tokens:
            break
        included.append(item)
        used += item_tokens
    return tuple(included), used


def _answer_prompt(
    question: str,
    evidence,
    guidance: tuple[DesktopKnowledgeGuidance, ...] = (),
    conversation_context: tuple[tuple[str, str], ...] = (),
    retrieval_trace: DesktopRetrievalTrace = DesktopRetrievalTrace(),
) -> str:
    prior = "\n\n".join(
        f"Previous user: {user}\nPrevious assistant: {assistant}"
        for user, assistant in conversation_context[-4:]
    )
    context = "\n".join(
        f"[{ordinal}] {reference.document_name} — {reference.section}\n{reference.excerpt}\n"
        for ordinal, reference in enumerate(evidence, start=1)
    )
    navigation = "\n\n".join(_guidance_text(item) for item in guidance)
    history = f"Conversation context:\n{prior}\n\n" if prior else ""
    guidance_section = (
        f"Knowledge Guidance (navigation only; not citation evidence):\n{navigation}\n\n"
        if navigation
        else ""
    )
    blueprint = _answer_blueprint(retrieval_trace, evidence)
    blueprint_section = (
        "Answer Blueprint (navigation only; labels and ordering are not citation evidence):\n"
        f"{blueprint}\n\n"
        if blueprint
        else ""
    )
    return (
        f"{history}Current question: {question}\n\n{guidance_section}{blueprint_section}"
        "Original Evidence is the only factual authority; cite it by number.\n"
        f"Evidence:\n{context}"
    )


def _answer_blueprint(
    trace: DesktopRetrievalTrace,
    evidence: tuple[DesktopEvidenceRef, ...],
) -> str:
    if not trace.coverage_aspects:
        return ""
    ordinals = {item.evidence_id: ordinal for ordinal, item in enumerate(evidence, start=1)}
    lines = [
        f"Answer kind: {trace.navigation_answer_kind or 'unspecified'}",
        f"Stop reason: {trace.navigation_stop_reason or 'unspecified'}",
    ]
    for ordinal, item in enumerate(trace.coverage_aspects, start=1):
        citations = [
            f"[{ordinals[evidence_id]}]"
            for evidence_id in item.evidence_ids
            if evidence_id in ordinals
        ]
        support = ", ".join(citations) if citations else "source gap"
        lines.append(f"{ordinal}. {item.aspect} — {item.status} — {support}")
    return "\n".join(lines)


def _coverage_after_grounding_budget(
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
    evidence_ids: set[str],
) -> tuple[DesktopAnswerCoverageTrace, ...]:
    values: list[DesktopAnswerCoverageTrace] = []
    for item in coverage:
        retained = tuple(
            evidence_id for evidence_id in item.evidence_ids if evidence_id in evidence_ids
        )
        status = item.status
        if status in {"covered", "partial"}:
            if not retained:
                status = "missing"
            elif len(retained) < len(item.evidence_ids):
                status = "partial"
        values.append(DesktopAnswerCoverageTrace(item.aspect, status, retained))
    return tuple(values)


def _coverage_state(coverage: tuple[DesktopAnswerCoverageTrace, ...]) -> str:
    if coverage and all(item.status in {"covered", "not_applicable"} for item in coverage):
        return "covered"
    if any(item.status in {"covered", "partial"} for item in coverage):
        return "partial"
    return "uncovered"


def _guidance_text(item: DesktopKnowledgeGuidance) -> str:
    return (
        f"Route: {item.route}\nTitle: {item.title}\nKind: {item.kind}\n"
        f"Authority: {item.authority}\n{item.content_markdown}"
    )


def _answer_context_capacity(gateway: DesktopModelGateway | None) -> int:
    if gateway is None:
        return _DEFAULT_ANSWER_CONTEXT_CAPACITY_TOKENS
    resolver = getattr(gateway, "capability_for_operation", None)
    if not callable(resolver):
        return _DEFAULT_ANSWER_CONTEXT_CAPACITY_TOKENS
    try:
        capability = resolver("grounded_answer")
    except Exception:
        return _DEFAULT_ANSWER_CONTEXT_CAPACITY_TOKENS
    capacity = getattr(capability, "context_capacity", None)
    return (
        capacity
        if isinstance(capacity, int) and capacity >= _DEFAULT_ANSWER_CONTEXT_CAPACITY_TOKENS
        else _DEFAULT_ANSWER_CONTEXT_CAPACITY_TOKENS
    )


def _deterministic_answer(question: str, evidence) -> str:
    if not evidence:
        return f"No available source evidence was found for: {question}"
    excerpts = "\n".join(
        f"[{ordinal}] {reference.excerpt}" for ordinal, reference in enumerate(evidence, start=1)
    )
    return f"Available source evidence for “{question}”:\n\n{excerpts}"


def _cancelled_generation(
    *, model_calls: int = 0, prompt: str | None = None
) -> DesktopGroundedAnswerGeneration:
    return DesktopGroundedAnswerGeneration(
        None,
        (),
        model_calls=model_calls,
        model_input_characters=(len(prompt) * model_calls) if prompt is not None else 0,
        interruption_code="answer_cancelled",
        interruption_reason="Answer generation was stopped.",
    )


def _is_cancelled(callback: AnswerCancellationCallback | None) -> bool:
    return callback is not None and callback()


def _stream_chunks(text: str):
    for offset in range(0, len(text), _STREAM_CHUNK_CHARS):
        yield text[offset : offset + _STREAM_CHUNK_CHARS]
