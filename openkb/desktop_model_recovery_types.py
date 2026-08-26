"""Neutral Recovery Assessment contract shared by current and retired model failures."""

from __future__ import annotations

from dataclasses import dataclass

CONTINUE_COMPATIBLE = "continue_compatible"
RESTART_CURRENT_PLAN = "restart_current_plan"
MODEL_RECOVERY_CHOICES = frozenset({CONTINUE_COMPATIBLE, RESTART_CURRENT_PLAN})


@dataclass(frozen=True)
class ModelRecoveryAssessment:
    compatible: bool
    compatibility_reason: str
    previous_prompt_digest: str | None
    provider: str | None
    model: str | None
    completed_batches: int
    total_batches: int
    continue_remaining_calls: int
    continue_input_tokens: int
    restart_remaining_calls: int
    restart_input_tokens: int
    recommended_choice: str
    selected_choice: str | None = None
    kind: str = "legacy_model_deadline"
    discarded_model_checkpoints: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "compatible": self.compatible,
            "compatibility_reason": self.compatibility_reason,
            "previous_prompt_digest": self.previous_prompt_digest,
            "provider": self.provider,
            "model": self.model,
            "completed_batches": self.completed_batches,
            "total_batches": self.total_batches,
            "choices": {
                CONTINUE_COMPATIBLE: {
                    "allowed": self.compatible,
                    "estimated_remaining_calls": self.continue_remaining_calls,
                    "estimated_input_tokens": self.continue_input_tokens,
                    "reuses_completed_batches": self.completed_batches,
                },
                RESTART_CURRENT_PLAN: {
                    "allowed": True,
                    "estimated_remaining_calls": self.restart_remaining_calls,
                    "estimated_input_tokens": self.restart_input_tokens,
                    "reuses_parser_document_ir_evidence": True,
                    "discarded_model_checkpoints": self.discarded_model_checkpoints,
                },
            },
            "recommended_choice": self.recommended_choice,
            "selected_choice": self.selected_choice,
            "discarded_model_checkpoints": self.discarded_model_checkpoints,
            "starts_automatically": False,
        }
