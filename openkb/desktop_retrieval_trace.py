"""Compact, immutable provenance for one Desktop Evidence Pack."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

FUSION_POLICY_VERSION = "openkb.rrf-protected-baseline-routed.v3"


@dataclass(frozen=True)
class DesktopRetrievalChannelTrace:
    """One retrieval channel's bounded contribution and degradation state."""

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
class DesktopAnswerCoverageTrace:
    """Source-content-free coverage state for one required answer aspect."""

    aspect: str
    status: str
    evidence_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "aspect": self.aspect,
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class DesktopRetrievalTrace:
    """Derived-generation identities and routing decisions retained by an answer."""

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
    coverage_gate_state: str = "not_applicable"
    navigation_answer_kind: str = ""
    navigation_subject: str = ""
    navigation_round_count: int = 0
    navigation_action_kinds: tuple[str, ...] = ()
    navigation_stop_reason: str = ""
    coverage_aspects: tuple[DesktopAnswerCoverageTrace, ...] = ()
    navigation_model_calls: int = 0
    navigation_logical_read_count: int = 0
    navigation_source_tokens: int = 0
    grounding_input_budget_tokens: int = 0
    evidence_input_tokens: int = 0
    guidance_input_tokens: int = 0

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
            "coverage_gate_state": self.coverage_gate_state,
            "navigation_answer_kind": self.navigation_answer_kind,
            "navigation_subject": self.navigation_subject,
            "navigation_round_count": self.navigation_round_count,
            "navigation_action_kinds": list(self.navigation_action_kinds),
            "navigation_stop_reason": self.navigation_stop_reason,
            "coverage_aspects": [item.as_dict() for item in self.coverage_aspects],
            "navigation_model_calls": self.navigation_model_calls,
            "navigation_logical_read_count": self.navigation_logical_read_count,
            "navigation_source_tokens": self.navigation_source_tokens,
            "grounding_input_budget_tokens": self.grounding_input_budget_tokens,
            "evidence_input_tokens": self.evidence_input_tokens,
            "guidance_input_tokens": self.guidance_input_tokens,
        }

    def with_canonical_evidence_ids(self, evidence_ids: tuple[str, ...]) -> DesktopRetrievalTrace:
        return replace(self, canonical_evidence_ids=tuple(dict.fromkeys(evidence_ids)))


def retrieval_trace_from_json(value: str) -> DesktopRetrievalTrace:
    """Read an optional historical trace without making old answers unreadable."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return DesktopRetrievalTrace()
    if not isinstance(payload, dict):
        return DesktopRetrievalTrace()
    channels = _channels(payload.get("channels"))
    fusion_policy = payload.get("fusion_policy_version")
    return DesktopRetrievalTrace(
        catalog_generation_ids=_strings(payload.get("catalog_generation_ids")),
        page_tree_generation_ids=_strings(payload.get("page_tree_generation_ids")),
        channels=channels,
        trigger_reasons=_strings(payload.get("trigger_reasons")),
        degradation_reasons=_strings(payload.get("degradation_reasons")),
        selected_node_ids=_strings(payload.get("selected_node_ids")),
        canonical_evidence_ids=_strings(payload.get("canonical_evidence_ids")),
        fusion_policy_version=fusion_policy if isinstance(fusion_policy, str) else "",
        navigation_snapshot_ids=_strings(payload.get("navigation_snapshot_ids")),
        navigation_routes=_strings(payload.get("navigation_routes")),
        navigation_read_count=_non_negative_int(payload.get("navigation_read_count")),
        source_window_count=_non_negative_int(payload.get("source_window_count")),
        link_hop_count=_non_negative_int(payload.get("link_hop_count")),
        page_tree_supplement_count=_non_negative_int(payload.get("page_tree_supplement_count")),
        coverage_gate_state=(
            str(payload["coverage_gate_state"])
            if isinstance(payload.get("coverage_gate_state"), str)
            else "not_applicable"
        ),
        navigation_answer_kind=_string(payload.get("navigation_answer_kind")),
        navigation_subject=_string(payload.get("navigation_subject")),
        navigation_round_count=_non_negative_int(payload.get("navigation_round_count")),
        navigation_action_kinds=_strings(payload.get("navigation_action_kinds")),
        navigation_stop_reason=_string(payload.get("navigation_stop_reason")),
        coverage_aspects=_coverage_aspects(payload.get("coverage_aspects")),
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


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _coverage_aspects(value: object) -> tuple[DesktopAnswerCoverageTrace, ...]:
    if not isinstance(value, list):
        return ()
    aspects: list[DesktopAnswerCoverageTrace] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        aspect = item.get("aspect")
        status = item.get("status")
        if (
            not isinstance(aspect, str)
            or not aspect
            or aspect in seen
            or status not in {"covered", "partial", "missing", "not_applicable"}
        ):
            continue
        seen.add(aspect)
        aspects.append(
            DesktopAnswerCoverageTrace(
                aspect=aspect,
                status=status,
                evidence_ids=_strings(item.get("evidence_ids")),
            )
        )
    return tuple(aspects)


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
