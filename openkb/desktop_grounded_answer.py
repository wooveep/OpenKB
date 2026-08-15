"""Grounded-answer orchestration over vectorless Desktop Evidence Packs."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from threading import Lock

from openkb.desktop_answer_store import DesktopGroundedAnswerStore, new_answer
from openkb.desktop_answer_types import DesktopGroundedAnswer
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelGateway,
    DesktopModelRequest,
)
from openkb.desktop_retrieval import DesktopEvidenceRetriever

AnswerDeltaCallback = Callable[[str, str, bool, int], None]
_MAX_CONTEXT_CHARS = 12_000
_STREAM_CHUNK_CHARS = 96


class DesktopGroundedAnswerService:
    """Answer from cited Available Knowledge, with deterministic fallback always available."""

    def __init__(self, kb_dir: Path, *, model_gateway: DesktopModelGateway | None = None) -> None:
        self._retriever = DesktopEvidenceRetriever(kb_dir, model_gateway=model_gateway)
        self._store = DesktopGroundedAnswerStore(kb_dir)
        self._model_gateway = model_gateway

    def answer(
        self, question: str, *, on_delta: AnswerDeltaCallback | None = None
    ) -> DesktopGroundedAnswer:
        """Stream a completed, persisted answer; optional model failures retain the baseline."""
        pack = self._retriever.retrieve(question)
        answer_id = uuid.uuid4().hex
        emitted = False
        visible_attempt = 0
        replace_pending = False
        stream_state_lock = Lock()

        def emit(delta: str, attempt: int, *, replace: bool = False) -> None:
            nonlocal emitted, replace_pending, visible_attempt
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
                if delta:
                    emitted = True
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

        answer_text, answer_degradation = self._answer_text(
            question,
            pack,
            on_delta=lambda attempt, delta: emit(delta, attempt),
            on_reset=reset,
        )
        with stream_state_lock:
            final_attempt = max(visible_attempt, 1)
            needs_fallback_stream = not emitted or bool(answer_degradation)
            if answer_degradation and visible_attempt:
                final_attempt = visible_attempt + 1
        if needs_fallback_stream:
            for ordinal, chunk in enumerate(_stream_chunks(answer_text)):
                emit(
                    chunk,
                    final_attempt,
                    replace=bool(answer_degradation and ordinal == 0),
                )
        answer = new_answer(
            answer_id=answer_id,
            question=pack.retrieval_plan.query,
            answer_text=answer_text,
            retrieval_plan=pack.retrieval_plan,
            citations=pack.evidence,
            degradations=tuple((*pack.degradations, *answer_degradation)),
            source_images=pack.source_images,
        )
        return self._store.save(answer)

    def list(self) -> tuple[DesktopGroundedAnswer, ...]:
        return self._store.list()

    def _answer_text(
        self,
        question: str,
        pack,
        *,
        on_delta: Callable[[int, str], None],
        on_reset: Callable[[int], None],
    ) -> tuple[str, tuple[str, ...]]:
        if self._model_gateway is None:
            return _deterministic_answer(question, pack.evidence), ("answer_model_unavailable",)
        try:
            result = self._model_gateway.stream(
                DesktopModelRequest(
                    "grounded_answer",
                    "Grounded answer",
                    _answer_prompt(question, pack.evidence),
                ),
                on_event=lambda _event: None,
                on_delta=on_delta,
                on_reset=on_reset,
            )
            answer_text = result.content.strip()
            if answer_text:
                return answer_text, ()
            return _deterministic_answer(question, pack.evidence), ("answer_model_fallback",)
        except DesktopModelCallError:
            return _deterministic_answer(question, pack.evidence), ("answer_model_fallback",)


def _answer_prompt(question: str, evidence) -> str:
    context: list[str] = []
    used = 0
    for ordinal, reference in enumerate(evidence, start=1):
        item = f"[{ordinal}] {reference.document_name} — {reference.section}\n{reference.excerpt}\n"
        if used + len(item) > _MAX_CONTEXT_CHARS:
            break
        context.append(item)
        used += len(item)
    return f"Question: {question}\n\nEvidence:\n" + "\n".join(context)


def _deterministic_answer(question: str, evidence) -> str:
    if not evidence:
        return f"No available source evidence was found for: {question}"
    excerpts = "\n".join(
        f"[{ordinal}] {reference.excerpt}" for ordinal, reference in enumerate(evidence, start=1)
    )
    return f"Available source evidence for “{question}”:\n\n{excerpts}"


def _stream_chunks(text: str):
    for offset in range(0, len(text), _STREAM_CHUNK_CHARS):
        yield text[offset : offset + _STREAM_CHUNK_CHARS]
