"""Compact, immutable provenance for one Desktop Evidence Pack."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

FUSION_POLICY_VERSION = "openkb.rrf-protected-baseline.v1"


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
