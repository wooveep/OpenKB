"""Answer capacity policy distinguishes configured capacity from verified capacity."""

from __future__ import annotations

from types import SimpleNamespace

from openkb.desktop_answer_budget import DesktopAnswerBudget, answer_budget_for_gateway


class _AnswerGateway:
    def __init__(
        self,
        *,
        context_capacity: int,
        verified: bool,
        reasoning_allowance_tokens: int = 0,
        maximum_output_tokens: int | None = None,
    ) -> None:
        self._context_capacity = context_capacity
        self._verified = verified
        self._reasoning_allowance_tokens = reasoning_allowance_tokens
        self._maximum_output_tokens = maximum_output_tokens

    def answer_capability_verified(self) -> bool:
        return self._verified

    def capability_for_operation(self, operation: str):
        assert operation == "grounded_answer"
        return SimpleNamespace(context_capacity=self._context_capacity)

    def answer_capability_profile(self):
        return SimpleNamespace(
            reasoning_allowance_tokens=self._reasoning_allowance_tokens,
            maximum_output_tokens=self._maximum_output_tokens,
        )


def test_unverified_large_context_uses_the_conservative_answer_budget() -> None:
    assert answer_budget_for_gateway(
        _AnswerGateway(context_capacity=1_000_000, verified=False)
    ) == DesktopAnswerBudget(
        capability_verified=False,
        context_capacity_tokens=4_096,
        final_output_reserve_tokens=2_048,
        provider_output_ceiling_tokens=2_048,
        navigation_max_evidence_refs=64,
        navigation_max_source_tokens=24_000,
    )


def test_verified_large_context_shares_one_bounded_retrieval_and_generation_policy() -> None:
    assert answer_budget_for_gateway(
        _AnswerGateway(
            context_capacity=1_000_000,
            verified=True,
            reasoning_allowance_tokens=65_536,
            maximum_output_tokens=384_000,
        )
    ) == DesktopAnswerBudget(
        capability_verified=True,
        context_capacity_tokens=1_000_000,
        final_output_reserve_tokens=32_768,
        provider_output_ceiling_tokens=98_304,
        navigation_max_evidence_refs=256,
        navigation_max_source_tokens=192_000,
    )
