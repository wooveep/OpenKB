"""Bounded query planning with the physical Model Attempt cost retained."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from openkb.desktop_answer_types import DesktopRetrievalModelCost, DesktopRetrievalPlan
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
)
from openkb.desktop_retrieval_plan import deterministic_plan, model_plan, with_baseline_terms


@dataclass(frozen=True)
class DesktopRetrievalPlanningResult:
    """One reusable plan plus degradations and real provider-attempt cost."""

    plan: DesktopRetrievalPlan
    degradations: tuple[str, ...]
    model_cost: DesktopRetrievalModelCost = DesktopRetrievalModelCost()


def build_retrieval_plan(
    question: str,
    model_gateway: DesktopModelGateway | None,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> DesktopRetrievalPlanningResult:
    """Build one plan while retaining every physical retry in its cost."""
    fallback = deterministic_plan(question)
    if model_gateway is None:
        return DesktopRetrievalPlanningResult(fallback, ("retrieval_plan_unavailable",))
    attempts = 0
    response = ""

    def observe(event) -> None:
        nonlocal attempts
        if event.status == "running":
            attempts = max(attempts, event.attempt)

    try:
        result = model_gateway.analyze(
            DesktopModelRequest("retrieval_plan", "Grounded answer question", question),
            on_event=observe,
            is_cancelled=is_cancelled,
        )
        attempts = max(attempts, result.attempt_count)
        response = result.content
        planned = model_plan(question, response)
        return DesktopRetrievalPlanningResult(
            with_baseline_terms(fallback, planned),
            (),
            _planning_cost(question, response, attempts),
        )
    except DesktopModelCancelledError:
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_cancelled",),
            _planning_cost(question, response, attempts),
        )
    except DesktopModelCallError:
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_fallback",),
            _planning_cost(question, response, attempts),
        )
    except (ValueError, json.JSONDecodeError):
        return DesktopRetrievalPlanningResult(
            fallback,
            ("retrieval_plan_fallback",),
            _planning_cost(question, response, attempts),
        )


def _planning_cost(question: str, response: str, attempts: int) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=attempts,
        input_characters=len(question) * attempts,
        output_characters=len(response),
    )
