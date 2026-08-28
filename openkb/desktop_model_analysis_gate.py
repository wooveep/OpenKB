"""Exact-profile capability gate shared by structured Analysis workloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_knowledge_analysis_plan import KnowledgeAnalysisPlan
from openkb.desktop_model_capabilities import DesktopModelCapabilityProfile
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_execution_profile import DesktopModelExecutionProfile
from openkb.desktop_model_gateway import DesktopModelCallError
from openkb.desktop_model_result_failure import (
    suspend_analysis_operation_failure,
)


@dataclass(frozen=True)
class DesktopAnalysisCapabilityGate:
    """Resolve and continuously honor one Analysis profile verification."""

    kb_dir: Path
    profile: DesktopModelExecutionProfile | None
    required: bool

    @classmethod
    def for_gateway(
        cls,
        kb_dir: Path,
        gateway: object,
        *,
        pinned_profile: DesktopModelExecutionProfile | None = None,
    ) -> DesktopAnalysisCapabilityGate:
        profile_factory = getattr(gateway, "execution_profile_for_operation", None)
        profile = (
            pinned_profile
            if pinned_profile is not None
            else profile_factory("knowledge_analysis")
            if callable(profile_factory)
            else None
        )
        return cls(
            kb_dir,
            profile,
            bool(getattr(gateway, "requires_analysis_capability_check", False)),
        )

    @property
    def verified(self) -> bool:
        if not self.required:
            return True
        return self.profile is not None and DesktopModelCapabilityStore(self.kb_dir).is_verified(
            self.profile
        )

    def suspend_result_failure(self, gateway: object, error: DesktopModelCallError) -> None:
        """Route a workload failure through operation-local/shared classification."""
        suspend_analysis_operation_failure(self.kb_dir, gateway, error)

    def invalidate_failure(self, failure_code: str, *, reason: str) -> None:
        """Forget shared evidence only for confirmed credential/configuration failures."""
        if self.profile is None or failure_code not in {
            "model_authentication_failed",
            "model_configuration_invalid",
        }:
            return
        DesktopModelCapabilityStore(self.kb_dir).invalidate(
            self.profile,
            failure_code=failure_code,
            reason=reason,
        )


@dataclass(frozen=True)
class DesktopImportAnalysisExecution:
    """Select current or persisted Analysis provenance before an import resumes."""

    gate: DesktopAnalysisCapabilityGate
    provider: str
    model: str
    capability: DesktopModelCapabilityProfile | None

    @classmethod
    def resolve(
        cls,
        kb_dir: Path,
        gateway: object,
        persisted_plan: KnowledgeAnalysisPlan | None,
    ) -> DesktopImportAnalysisExecution:
        pinned = (
            persisted_plan
            if persisted_plan is not None and persisted_plan.execution_profile is not None
            else None
        )
        gate = DesktopAnalysisCapabilityGate.for_gateway(
            kb_dir,
            gateway,
            pinned_profile=pinned.execution_profile if pinned is not None else None,
        )
        capability_factory = getattr(gateway, "capability_for_operation", None)
        return cls(
            gate,
            pinned.provider if pinned is not None else str(getattr(gateway, "provider_name")),
            pinned.analysis_model if pinned is not None else str(getattr(gateway, "model_name")),
            pinned.capability_profile
            if pinned is not None
            else capability_factory("knowledge_analysis")
            if callable(capability_factory)
            else None,
        )
