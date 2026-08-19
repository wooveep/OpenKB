"""Bounded deterministic and optional-model query planning for Desktop retrieval."""

from __future__ import annotations

import json
import re

from openkb.desktop_answer_types import DesktopAnswerError, DesktopRetrievalPlan
from openkb.desktop_lexical import cjk_bigrams, is_cjk_text

_MAX_QUERY_LENGTH = 2_000
_MAX_PLAN_TERMS = 8
_MAX_COMBINED_PLAN_TERMS = 12
_TERM_PATTERN = re.compile(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]+")


def validate_question(question: str) -> str:
    if not isinstance(question, str) or not question.strip():
        raise DesktopAnswerError("invalid_question", "Enter a question before asking OpenKB.")
    normalized = " ".join(question.split())
    if len(normalized) > _MAX_QUERY_LENGTH:
        raise DesktopAnswerError(
            "invalid_question", "The question is too long for grounded retrieval."
        )
    return normalized


def deterministic_plan(question: str) -> DesktopRetrievalPlan:
    return DesktopRetrievalPlan(query=question, terms=terms(question), source="deterministic")


def model_plan(question: str, content: str) -> DesktopRetrievalPlan:
    payload = json.loads(_json_object_text(content))
    if not isinstance(payload, dict):
        raise ValueError("Retrieval Plan must be an object.")
    values = payload.get("terms")
    if not isinstance(values, list):
        raise ValueError("Retrieval Plan terms are missing.")
    planned_terms = terms(" ".join(value for value in values if isinstance(value, str)))
    if not planned_terms:
        raise ValueError("Retrieval Plan terms are empty.")
    return DesktopRetrievalPlan(query=question, terms=planned_terms, source="model")


def with_baseline_terms(
    baseline: DesktopRetrievalPlan, model: DesktopRetrievalPlan
) -> DesktopRetrievalPlan:
    """Keep deterministic question terms even when a valid model plan is incomplete."""
    combined: list[str] = []
    for value in (*baseline.terms, *model.terms):
        if value not in combined:
            combined.append(value)
        if len(combined) == _MAX_COMBINED_PLAN_TERMS:
            break
    return DesktopRetrievalPlan(
        query=baseline.query,
        terms=tuple(combined),
        source=model.source,
    )


def terms(value: str) -> tuple[str, ...]:
    selected: list[str] = []
    for match in _TERM_PATTERN.finditer(value.casefold()):
        token = match.group(0)
        values = cjk_bigrams(token) if is_cjk_text(token) else (token,)
        for item in values:
            if item and item not in selected:
                selected.append(item)
            if len(selected) == _MAX_PLAN_TERMS:
                return tuple(selected)
    return tuple(selected)


def _json_object_text(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0].strip()
    return stripped
