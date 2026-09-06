"""Caller budgets govern new dispatches, never an established model attempt."""

from __future__ import annotations

import time


class ModelDispatchBudgetExhausted(RuntimeError):
    """The caller has no budget to start another operation or repair."""


def require_model_dispatch_budget(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise ModelDispatchBudgetExhausted()
