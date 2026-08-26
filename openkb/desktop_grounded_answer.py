"""Grounded-answer orchestration over vectorless Desktop Evidence Packs."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import cast

from openkb.desktop_answer_store import DesktopGroundedAnswerStore, new_answer
from openkb.desktop_answer_types import (
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopGroundedAnswer,
)
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_answer_capability_verified,
)
from openkb.desktop_retrieval import DesktopEvidenceRetriever

AnswerDeltaCallback = Callable[[str, str, bool, int], None]
AnswerCancellationCallback = Callable[[], bool]
AnswerModelEventCallback = Callable[[object], None]
_MAX_CONTEXT_CHARS = 12_000
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
    ) -> DesktopGroundedAnswer:
        """Generate an auditable answer without writing the legacy flat-answer tables."""
        return self._attempt(
            question,
            answer_id=uuid.uuid4().hex,
            on_delta=on_delta,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
            conversation_context=conversation_context,
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
    ) -> DesktopGroundedAnswer:
        pack = prepare_grounded_evidence_pack(
            self._retriever.retrieve(
                question,
                is_cancelled=is_cancelled,
                on_model_event=on_model_event,
            )
        )
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


def prepare_grounded_evidence_pack(pack: DesktopEvidencePack) -> DesktopEvidencePack:
    """Apply the exact production context bound before generation and scoring."""
    sent_evidence = _evidence_for_prompt(pack.evidence)
    sent_evidence_ids = {reference.evidence_id for reference in sent_evidence}
    return DesktopEvidencePack(
        retrieval_plan=pack.retrieval_plan,
        evidence=sent_evidence,
        degradations=pack.degradations,
        source_images=tuple(
            image for image in pack.source_images if image.evidence_id in sent_evidence_ids
        ),
        retrieval_trace=pack.retrieval_trace.with_canonical_evidence_ids(
            tuple(reference.evidence_id for reference in sent_evidence)
        ),
        retrieval_model_cost=pack.retrieval_model_cost,
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

    prompt = _answer_prompt(question, pack.evidence, conversation_context)
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
) -> tuple[DesktopEvidenceRef, ...]:
    included: list[DesktopEvidenceRef] = []
    used = 0
    for ordinal, reference in enumerate(evidence, start=1):
        item = f"[{ordinal}] {reference.document_name} — {reference.section}\n{reference.excerpt}\n"
        if used + len(item) > _MAX_CONTEXT_CHARS:
            break
        included.append(reference)
        used += len(item)
    return tuple(included)


def _answer_prompt(
    question: str,
    evidence,
    conversation_context: tuple[tuple[str, str], ...] = (),
) -> str:
    prior = "\n\n".join(
        f"Previous user: {user}\nPrevious assistant: {assistant}"
        for user, assistant in conversation_context[-4:]
    )
    context = "\n".join(
        f"[{ordinal}] {reference.document_name} — {reference.section}\n{reference.excerpt}\n"
        for ordinal, reference in enumerate(evidence, start=1)
    )
    history = f"Conversation context:\n{prior}\n\n" if prior else ""
    return f"{history}Current question: {question}\n\nEvidence:\n{context}"


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
