"""Immutable token-budgeted plans for resumable Knowledge Analysis."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from openkb.importing.artifacts import DocumentIRBlock
from openkb.models.capabilities import DesktopModelCapabilityProfile
from openkb.models.execution_profile import (
    MINIMUM_ANALYSIS_FINAL_OUTPUT_TOKENS,
    DesktopModelExecutionProfile,
    analysis_prompt_contract_bundle,
)
from openkb.models.execution_profile import (
    estimate_model_tokens as _estimate_model_tokens,
)
from openkb.models.prompt_contracts import DesktopPromptContract

estimate_model_tokens = _estimate_model_tokens


@dataclass(frozen=True)
class KnowledgeAnalysisBatchPlan:
    ordinal: int
    evidence_ids: tuple[str, ...]
    section_paths: tuple[tuple[str, ...], ...]
    estimated_input_tokens: int

    def as_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "evidence_ids": list(self.evidence_ids),
            "section_paths": [list(path) for path in self.section_paths],
            "estimated_input_tokens": self.estimated_input_tokens,
        }


@dataclass(frozen=True)
class KnowledgeAnalysisMergeNodePlan:
    node_id: str
    level: int
    ordinal: int
    child_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "level": self.level,
            "ordinal": self.ordinal,
            "child_ids": list(self.child_ids),
        }


@dataclass(frozen=True)
class KnowledgeAnalysisPlan:
    document_ir_digest: str
    provider: str
    analysis_model: str
    capability_profile: DesktopModelCapabilityProfile
    prompt_contract_snapshot: dict[str, object]
    prompt_contract_digest: str
    input_budget_tokens: int
    output_budget_tokens: int
    final_output_reserve_tokens: int
    reasoning_allowance_tokens: int
    execution_profile: DesktopModelExecutionProfile | None
    batches: tuple[KnowledgeAnalysisBatchPlan, ...]
    merge_topology: tuple[KnowledgeAnalysisMergeNodePlan, ...]

    @property
    def plan_identity(self) -> str:
        return hashlib.sha256(_json(self._identity_payload()).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_payload(), "plan_identity": self.plan_identity}

    def _identity_payload(self) -> dict[str, object]:
        return {
            "document_ir_digest": self.document_ir_digest,
            "provider": self.provider,
            "analysis_model": self.analysis_model,
            "capability_profile": {
                "context_capacity": self.capability_profile.context_capacity,
                "document_input_capacity": self.capability_profile.document_input_capacity,
                "supports_native_json_schema": (
                    self.capability_profile.supports_native_json_schema
                ),
                "supports_streaming": self.capability_profile.supports_streaming,
                "supports_reasoning": self.capability_profile.supports_reasoning,
            },
            "prompt_contract_snapshot": self.prompt_contract_snapshot,
            "prompt_contract_digest": self.prompt_contract_digest,
            "input_budget_tokens": self.input_budget_tokens,
            "output_budget_tokens": self.output_budget_tokens,
            "final_output_reserve_tokens": self.final_output_reserve_tokens,
            "reasoning_allowance_tokens": self.reasoning_allowance_tokens,
            "execution_profile": (
                self.execution_profile.as_dict() if self.execution_profile is not None else None
            ),
            "batches": [batch.as_dict() for batch in self.batches],
            "merge_topology": [node.as_dict() for node in self.merge_topology],
        }

    @classmethod
    def from_dict(cls, value: object) -> KnowledgeAnalysisPlan:
        if not isinstance(value, dict):
            raise ValueError("Knowledge Analysis Plan must be an object.")
        capability = _mapping(value.get("capability_profile"), "capability_profile")
        batches = _list(value.get("batches"), "batches")
        topology = _list(value.get("merge_topology"), "merge_topology")
        snapshot = _mapping(value.get("prompt_contract_snapshot"), "prompt_contract_snapshot")
        raw_profile = value.get("execution_profile")
        plan = cls(
            document_ir_digest=_string(value, "document_ir_digest"),
            provider=_string(value, "provider"),
            analysis_model=_string(value, "analysis_model"),
            capability_profile=DesktopModelCapabilityProfile(
                context_capacity=_integer(capability, "context_capacity"),
                document_input_capacity=_integer(capability, "document_input_capacity"),
                supports_native_json_schema=_boolean(capability, "supports_native_json_schema"),
                supports_streaming=_boolean(capability, "supports_streaming"),
                supports_reasoning=_boolean(capability, "supports_reasoning"),
            ),
            prompt_contract_snapshot=dict(snapshot),
            prompt_contract_digest=_string(value, "prompt_contract_digest"),
            input_budget_tokens=_integer(value, "input_budget_tokens"),
            output_budget_tokens=_integer(value, "output_budget_tokens"),
            final_output_reserve_tokens=_optional_integer(
                value,
                "final_output_reserve_tokens",
                _integer(value, "output_budget_tokens"),
            ),
            reasoning_allowance_tokens=_optional_integer(value, "reasoning_allowance_tokens", 0),
            execution_profile=(
                DesktopModelExecutionProfile.from_dict(raw_profile)
                if raw_profile is not None
                else None
            ),
            batches=tuple(_batch_from_dict(item) for item in batches),
            merge_topology=tuple(_merge_node_from_dict(item) for item in topology),
        )
        stored_identity = value.get("plan_identity")
        if stored_identity is not None and stored_identity != plan.plan_identity:
            raise ValueError("Knowledge Analysis Plan identity does not match its fields.")
        return plan


def knowledge_analysis_input_budget(
    capability: DesktopModelCapabilityProfile, contract: DesktopPromptContract
) -> int:
    """Use verified operation capacity after reserving the contract's bounded output."""
    output_budget = knowledge_analysis_output_budget(capability, contract)
    return min(
        capability.document_input_capacity,
        max(1, capability.context_capacity - output_budget),
    )


def knowledge_analysis_output_budget(
    capability: DesktopModelCapabilityProfile, contract: DesktopPromptContract
) -> int:
    """Use the contract reserve when it fits, otherwise preserve half the context for input."""
    requested = _positive_int(
        contract.token_budget_policy.get("reserve_output_tokens"),
        4_096,
    )
    return min(
        requested,
        max(
            MINIMUM_ANALYSIS_FINAL_OUTPUT_TOKENS,
            capability.context_capacity // 2,
        ),
    )


def build_knowledge_analysis_plan(
    *,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    planned_batches: tuple[tuple[tuple[str, DocumentIRBlock], ...], ...],
    provider: str,
    model: str,
    capability: DesktopModelCapabilityProfile,
    contract: DesktopPromptContract,
    estimated_batch_tokens: tuple[int, ...],
    execution_profile: DesktopModelExecutionProfile | None = None,
) -> KnowledgeAnalysisPlan:
    final_output_reserve = knowledge_analysis_output_budget(capability, contract)
    reasoning_allowance = 0
    output_budget = final_output_reserve
    input_budget = knowledge_analysis_input_budget(capability, contract)
    if execution_profile is not None:
        if (execution_profile.provider, execution_profile.model) != (provider, model):
            raise ValueError("Model Execution Profile does not match the Analysis selection.")
        final_output_reserve = execution_profile.final_output_reserve_tokens
        reasoning_allowance = execution_profile.reasoning_allowance_tokens
        output_budget = execution_profile.provider_output_ceiling_tokens
        input_budget = execution_profile.document_input_budget_tokens
    batch_plans = tuple(
        KnowledgeAnalysisBatchPlan(
            ordinal=ordinal,
            evidence_ids=tuple(item[0] for item in batch),
            section_paths=_section_paths(batch),
            estimated_input_tokens=estimated_batch_tokens[ordinal],
        )
        for ordinal, batch in enumerate(planned_batches)
    )
    snapshot_bundle = analysis_prompt_contract_bundle()
    bundle_digest = hashlib.sha256(_json(snapshot_bundle).encode("utf-8")).hexdigest()
    if execution_profile is not None and execution_profile.prompt_contract_digest != bundle_digest:
        raise ValueError("Model Execution Profile does not match the Prompt Contract bundle.")
    return KnowledgeAnalysisPlan(
        document_ir_digest=document_ir_digest(evidence),
        provider=provider,
        analysis_model=model,
        capability_profile=capability,
        prompt_contract_snapshot=snapshot_bundle,
        prompt_contract_digest=bundle_digest,
        input_budget_tokens=input_budget,
        output_budget_tokens=output_budget,
        final_output_reserve_tokens=final_output_reserve,
        reasoning_allowance_tokens=reasoning_allowance,
        execution_profile=execution_profile,
        batches=batch_plans,
        merge_topology=hierarchical_merge_topology(len(batch_plans)),
    )


def document_ir_digest(evidence: tuple[tuple[str, DocumentIRBlock], ...]) -> str:
    payload = [
        {
            "evidence_id": evidence_id,
            "block_id": block.block_id,
            "ordinal": block.ordinal,
            "kind": block.kind,
            "heading_path": list(block.heading_path),
            "locator": block.locator,
            "line_start": block.line_start,
            "line_end": block.line_end,
            "text_sha256": hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
        }
        for evidence_id, block in evidence
    ]
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def prompt_snapshot_for_operation(
    plan: KnowledgeAnalysisPlan,
    operation: str,
) -> dict[str, object]:
    contracts = plan.prompt_contract_snapshot.get("contracts")
    if not isinstance(contracts, dict):
        raise ValueError("Knowledge Analysis Plan prompt-contract bundle is invalid.")
    snapshot = contracts.get(operation)
    if not isinstance(snapshot, dict):
        raise ValueError(f"Knowledge Analysis Plan does not pin {operation}.")
    return snapshot


def hierarchical_merge_topology(
    leaf_count: int,
    *,
    fan_in: int = 4,
) -> tuple[KnowledgeAnalysisMergeNodePlan, ...]:
    if leaf_count <= 1:
        return ()
    children = [f"batch:{ordinal}" for ordinal in range(leaf_count)]
    nodes: list[KnowledgeAnalysisMergeNodePlan] = []
    level = 0
    while len(children) > 1:
        parents: list[str] = []
        for ordinal, start in enumerate(range(0, len(children), fan_in)):
            grouped_children = tuple(children[start : start + fan_in])
            if len(grouped_children) == 1:
                parents.append(grouped_children[0])
                continue
            node_id = f"merge:{level}:{ordinal}"
            nodes.append(
                KnowledgeAnalysisMergeNodePlan(
                    node_id=node_id,
                    level=level,
                    ordinal=ordinal,
                    child_ids=grouped_children,
                )
            )
            parents.append(node_id)
        children = parents
        level += 1
    return tuple(nodes)


def _section_paths(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(dict.fromkeys(block.heading_path for _evidence_id, block in evidence))


def _batch_from_dict(value: object) -> KnowledgeAnalysisBatchPlan:
    item = _mapping(value, "batch")
    paths = _list(item.get("section_paths"), "section_paths")
    evidence_ids = _list(item.get("evidence_ids"), "evidence_ids")
    return KnowledgeAnalysisBatchPlan(
        ordinal=_integer(item, "ordinal"),
        evidence_ids=tuple(
            _string_value(evidence_id, "evidence_id") for evidence_id in evidence_ids
        ),
        section_paths=tuple(
            tuple(_string_value(part, "section_path") for part in _list(path, "section_path"))
            for path in paths
        ),
        estimated_input_tokens=_integer(item, "estimated_input_tokens"),
    )


def _merge_node_from_dict(value: object) -> KnowledgeAnalysisMergeNodePlan:
    item = _mapping(value, "merge_node")
    return KnowledgeAnalysisMergeNodePlan(
        node_id=_string(item, "node_id"),
        level=_integer(item, "level"),
        ordinal=_integer(item, "ordinal"),
        child_ids=tuple(
            _string_value(child, "child_id") for child in _list(item.get("child_ids"), "child_ids")
        ),
    )


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Knowledge Analysis Plan {field} is invalid.")
    return value


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"Knowledge Analysis Plan {field} is invalid.")
    return value


def _string(mapping: dict[str, object], field: str) -> str:
    return _string_value(mapping.get(field), field)


def _string_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Knowledge Analysis Plan {field} is invalid.")
    return value


def _integer(mapping: dict[str, object], field: str) -> int:
    value = mapping.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Knowledge Analysis Plan {field} is invalid.")
    return value


def _optional_integer(mapping: dict[str, object], field: str, default: int) -> int:
    return default if field not in mapping else _integer(mapping, field)


def _boolean(mapping: dict[str, object], field: str) -> bool:
    value = mapping.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"Knowledge Analysis Plan {field} is invalid.")
    return value


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
