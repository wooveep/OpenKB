"""Compact, immutable provenance for one Desktop Evidence Pack."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

FUSION_POLICY_VERSION = "openkb.rrf-protected-baseline-routed.v3"


@dataclass(frozen=True)
class DesktopRetrievalChannelTrace:
    channel: str
    candidate_count: int
    trigger_reasons: tuple[str, ...] = ()
    degradation_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "candidate_count": self.candidate_count,
            "trigger_reasons": list(self.trigger_reasons),
            "degradation_reasons": list(self.degradation_reasons),
        }


@dataclass(frozen=True)
class DesktopQuestionFacetTrace:
    facet_id: str
    label: str
    description: str
    importance: str

    def as_dict(self) -> dict[str, str]:
        return {
            "facet_id": self.facet_id,
            "label": self.label,
            "description": self.description,
            "importance": self.importance,
        }


@dataclass(frozen=True)
class DesktopFacetCoverageTrace:
    facet_id: str
    state: str
    evidence_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "facet_id": self.facet_id,
            "state": self.state,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class DesktopRetrievalTrace:
    """Derived identities, dynamic Question Facets, and bounded routing decisions."""

    catalog_generation_ids: tuple[str, ...] = ()
    page_tree_generation_ids: tuple[str, ...] = ()
    channels: tuple[DesktopRetrievalChannelTrace, ...] = ()
    trigger_reasons: tuple[str, ...] = ()
    degradation_reasons: tuple[str, ...] = ()
    selected_node_ids: tuple[str, ...] = ()
    canonical_evidence_ids: tuple[str, ...] = ()
    fusion_policy_version: str = ""
    navigation_snapshot_ids: tuple[str, ...] = ()
    navigation_routes: tuple[str, ...] = ()
    navigation_read_count: int = 0
    source_window_count: int = 0
    link_hop_count: int = 0
    page_tree_supplement_count: int = 0
    semantic_structure_state: str = "unknown"
    question_goal: str = ""
    question_facets: tuple[DesktopQuestionFacetTrace, ...] = ()
    question_facet_plan_digest: str = ""
    query_planning_prompt_contract_digest: str = ""
    query_planning_execution_profile_json: str = ""
    query_planning_execution_profile_digest: str = ""
    facet_coverage: tuple[DesktopFacetCoverageTrace, ...] = ()
    coverage_gate_state: str = "unknown"
    navigation_round_count: int = 0
    navigation_action_kinds: tuple[str, ...] = ()
    navigation_stop_reason: str = ""
    navigation_model_calls: int = 0
    navigation_logical_read_count: int = 0
    navigation_source_tokens: int = 0
    grounding_input_budget_tokens: int = 0
    evidence_input_tokens: int = 0
    guidance_input_tokens: int = 0
    version_navigation_snapshot_id: str = ""
    version_catalog_revision_id: str = ""
    version_catalog_digest: str = ""
    version_scope_mode: str = ""
    version_scope_status: str = ""
    version_scope_lineage_ids: tuple[str, ...] = ()
    version_scope_labels: tuple[str, ...] = ()
    version_scope_document_ids: tuple[str, ...] = ()
    version_scope_selection_reason: str = ""
    version_scope_degradation_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "catalog_generation_ids": list(self.catalog_generation_ids),
            "page_tree_generation_ids": list(self.page_tree_generation_ids),
            "channels": [channel.as_dict() for channel in self.channels],
            "trigger_reasons": list(self.trigger_reasons),
            "degradation_reasons": list(self.degradation_reasons),
            "selected_node_ids": list(self.selected_node_ids),
            "canonical_evidence_ids": list(self.canonical_evidence_ids),
            "fusion_policy_version": self.fusion_policy_version,
            "navigation_snapshot_ids": list(self.navigation_snapshot_ids),
            "navigation_routes": list(self.navigation_routes),
            "navigation_read_count": self.navigation_read_count,
            "source_window_count": self.source_window_count,
            "link_hop_count": self.link_hop_count,
            "page_tree_supplement_count": self.page_tree_supplement_count,
            "semantic_structure_state": self.semantic_structure_state,
            "question_goal": self.question_goal,
            "question_facets": [item.as_dict() for item in self.question_facets],
            "question_facet_plan_digest": self.question_facet_plan_digest,
            "query_planning_prompt_contract_digest": (self.query_planning_prompt_contract_digest),
            "query_planning_execution_profile_json": (self.query_planning_execution_profile_json),
            "query_planning_execution_profile_digest": (
                self.query_planning_execution_profile_digest
            ),
            "facet_coverage": [item.as_dict() for item in self.facet_coverage],
            "coverage_gate_state": self.coverage_gate_state,
            "navigation_round_count": self.navigation_round_count,
            "navigation_action_kinds": list(self.navigation_action_kinds),
            "navigation_stop_reason": self.navigation_stop_reason,
            "navigation_model_calls": self.navigation_model_calls,
            "navigation_logical_read_count": self.navigation_logical_read_count,
            "navigation_source_tokens": self.navigation_source_tokens,
            "grounding_input_budget_tokens": self.grounding_input_budget_tokens,
            "evidence_input_tokens": self.evidence_input_tokens,
            "guidance_input_tokens": self.guidance_input_tokens,
            "version_navigation_snapshot_id": self.version_navigation_snapshot_id,
            "version_catalog_revision_id": self.version_catalog_revision_id,
            "version_catalog_digest": self.version_catalog_digest,
            "version_scope_mode": self.version_scope_mode,
            "version_scope_status": self.version_scope_status,
            "version_scope_lineage_ids": list(self.version_scope_lineage_ids),
            "version_scope_labels": list(self.version_scope_labels),
            "version_scope_document_ids": list(self.version_scope_document_ids),
            "version_scope_selection_reason": self.version_scope_selection_reason,
            "version_scope_degradation_reason": self.version_scope_degradation_reason,
        }

    def with_canonical_evidence_ids(self, evidence_ids: tuple[str, ...]) -> DesktopRetrievalTrace:
        return replace(self, canonical_evidence_ids=tuple(dict.fromkeys(evidence_ids)))


def retrieval_trace_from_json(value: str) -> DesktopRetrievalTrace:
    """Read one current-epoch trace; obsolete schemas are intentionally not adapted."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return DesktopRetrievalTrace()
    if not isinstance(payload, dict):
        return DesktopRetrievalTrace()
    state = payload.get("semantic_structure_state")
    if state not in {"known", "unknown"}:
        state = "unknown"
    return DesktopRetrievalTrace(
        catalog_generation_ids=_strings(payload.get("catalog_generation_ids")),
        page_tree_generation_ids=_strings(payload.get("page_tree_generation_ids")),
        channels=_channels(payload.get("channels")),
        trigger_reasons=_strings(payload.get("trigger_reasons")),
        degradation_reasons=_strings(payload.get("degradation_reasons")),
        selected_node_ids=_strings(payload.get("selected_node_ids")),
        canonical_evidence_ids=_strings(payload.get("canonical_evidence_ids")),
        fusion_policy_version=_string(payload.get("fusion_policy_version")),
        navigation_snapshot_ids=_strings(payload.get("navigation_snapshot_ids")),
        navigation_routes=_strings(payload.get("navigation_routes")),
        navigation_read_count=_non_negative_int(payload.get("navigation_read_count")),
        source_window_count=_non_negative_int(payload.get("source_window_count")),
        link_hop_count=_non_negative_int(payload.get("link_hop_count")),
        page_tree_supplement_count=_non_negative_int(payload.get("page_tree_supplement_count")),
        semantic_structure_state=state,
        question_goal=_string(payload.get("question_goal")),
        question_facets=_question_facets(payload.get("question_facets")),
        question_facet_plan_digest=_string(payload.get("question_facet_plan_digest")),
        query_planning_prompt_contract_digest=_string(
            payload.get("query_planning_prompt_contract_digest")
        ),
        query_planning_execution_profile_json=_string(
            payload.get("query_planning_execution_profile_json")
        ),
        query_planning_execution_profile_digest=_string(
            payload.get("query_planning_execution_profile_digest")
        ),
        facet_coverage=_facet_coverage(payload.get("facet_coverage")),
        coverage_gate_state=_string(payload.get("coverage_gate_state")) or "unknown",
        navigation_round_count=_non_negative_int(payload.get("navigation_round_count")),
        navigation_action_kinds=_strings(payload.get("navigation_action_kinds")),
        navigation_stop_reason=_string(payload.get("navigation_stop_reason")),
        navigation_model_calls=_non_negative_int(payload.get("navigation_model_calls")),
        navigation_logical_read_count=_non_negative_int(
            payload.get("navigation_logical_read_count")
        ),
        navigation_source_tokens=_non_negative_int(payload.get("navigation_source_tokens")),
        grounding_input_budget_tokens=_non_negative_int(
            payload.get("grounding_input_budget_tokens")
        ),
        evidence_input_tokens=_non_negative_int(payload.get("evidence_input_tokens")),
        guidance_input_tokens=_non_negative_int(payload.get("guidance_input_tokens")),
        version_navigation_snapshot_id=_string(payload.get("version_navigation_snapshot_id")),
        version_catalog_revision_id=_string(payload.get("version_catalog_revision_id")),
        version_catalog_digest=_string(payload.get("version_catalog_digest")),
        version_scope_mode=_string(payload.get("version_scope_mode")),
        version_scope_status=_string(payload.get("version_scope_status")),
        version_scope_lineage_ids=_strings(payload.get("version_scope_lineage_ids")),
        version_scope_labels=_strings(payload.get("version_scope_labels")),
        version_scope_document_ids=_strings(payload.get("version_scope_document_ids")),
        version_scope_selection_reason=_string(payload.get("version_scope_selection_reason")),
        version_scope_degradation_reason=_string(payload.get("version_scope_degradation_reason")),
    )


def _channels(value: object) -> tuple[DesktopRetrievalChannelTrace, ...]:
    if not isinstance(value, list):
        return ()
    channels: list[DesktopRetrievalChannelTrace] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("channel"), str):
            continue
        count = item.get("candidate_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        channels.append(
            DesktopRetrievalChannelTrace(
                channel=item["channel"],
                candidate_count=count,
                trigger_reasons=_strings(item.get("trigger_reasons")),
                degradation_reasons=_strings(item.get("degradation_reasons")),
            )
        )
    return tuple(channels)


def _question_facets(value: object) -> tuple[DesktopQuestionFacetTrace, ...]:
    if not isinstance(value, list):
        return ()
    result: list[DesktopQuestionFacetTrace] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "facet_id",
            "label",
            "description",
            "importance",
        }:
            continue
        if not all(isinstance(item[field], str) and item[field] for field in item):
            continue
        if item["importance"] not in {"required", "supporting"}:
            continue
        result.append(DesktopQuestionFacetTrace(**item))
    return tuple(result)


def _facet_coverage(value: object) -> tuple[DesktopFacetCoverageTrace, ...]:
    if not isinstance(value, list):
        return ()
    result: list[DesktopFacetCoverageTrace] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"facet_id", "state", "evidence_ids"}:
            continue
        facet_id = item.get("facet_id")
        state = item.get("state")
        if (
            not isinstance(facet_id, str)
            or not facet_id
            or facet_id in seen
            or state not in {"covered", "partial", "missing"}
        ):
            continue
        seen.add(facet_id)
        result.append(
            DesktopFacetCoverageTrace(facet_id, str(state), _strings(item.get("evidence_ids")))
        )
    return tuple(result)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
