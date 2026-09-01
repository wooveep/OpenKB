"""Kind-specific readable Markdown for evidence-bound generated knowledge."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

_CJK = re.compile(r"[\u3400-\u9fff]")
UNSPECIFIED_APPLICABILITY = "Unspecified"


@dataclass(frozen=True)
class RenderedKnowledgeClaim:
    text: str
    role: str
    source_markers: tuple[str, ...]
    applicability: tuple[tuple[str, str], ...] = ()


_ZH_SECTIONS = {
    "concept": (
        ("定义与说明", ("definition", "purpose")),
        ("机制与能力", ("mechanism", "capability")),
        ("适用范围", ("scope",)),
        ("权衡与限制", ("limitation",)),
        ("关联知识", ("relation",)),
        (
            "补充信息",
            ("detail", "prerequisite", "step", "validation", "rollback", "troubleshooting"),
        ),
    ),
    "entity": (
        ("定位与作用", ("definition", "purpose")),
        ("能力与机制", ("capability", "mechanism")),
        ("适用范围", ("scope",)),
        ("相关操作", ("prerequisite", "step", "validation", "rollback", "troubleshooting")),
        ("限制与关联", ("limitation", "relation")),
        ("补充信息", ("detail",)),
    ),
    "procedure": (
        ("目标", ("purpose", "definition")),
        ("适用范围", ("scope",)),
        ("前置条件", ("prerequisite",)),
        ("操作步骤", ("step",)),
        ("验证", ("validation",)),
        ("回滚", ("rollback",)),
        ("故障排查", ("troubleshooting",)),
        ("限制与关联", ("limitation", "relation", "mechanism", "capability", "detail")),
    ),
}

_EN_SECTIONS = {
    "concept": (
        ("Definition", ("definition", "purpose")),
        ("Mechanism and capabilities", ("mechanism", "capability")),
        ("Applicability", ("scope",)),
        ("Trade-offs and limitations", ("limitation",)),
        ("Related knowledge", ("relation",)),
        (
            "Details",
            ("detail", "prerequisite", "step", "validation", "rollback", "troubleshooting"),
        ),
    ),
    "entity": (
        ("Identity and role", ("definition", "purpose")),
        ("Capabilities and mechanism", ("capability", "mechanism")),
        ("Applicability", ("scope",)),
        (
            "Related operations",
            ("prerequisite", "step", "validation", "rollback", "troubleshooting"),
        ),
        ("Limitations and relations", ("limitation", "relation")),
        ("Details", ("detail",)),
    ),
    "procedure": (
        ("Goal", ("purpose", "definition")),
        ("Applicability", ("scope",)),
        ("Prerequisites", ("prerequisite",)),
        ("Steps", ("step",)),
        ("Validation", ("validation",)),
        ("Rollback", ("rollback",)),
        ("Troubleshooting", ("troubleshooting",)),
        (
            "Limitations and relations",
            ("limitation", "relation", "mechanism", "capability", "detail"),
        ),
    ),
}


def render_generated_knowledge(
    kind: str,
    claims: tuple[RenderedKnowledgeClaim, ...],
    *,
    language: str | None = None,
) -> str:
    """Render each factual unit once while preserving its compact source markers."""
    chinese = language == "zh" or (
        language is None
        and sum(bool(_CJK.search(claim.text)) for claim in claims) * 2 >= max(1, len(claims))
    )
    sections = (_ZH_SECTIONS if chinese else _EN_SECTIONS)[kind]
    by_role: dict[str, list[RenderedKnowledgeClaim]] = defaultdict(list)
    for claim in claims:
        by_role[claim.role if claim.role in _known_roles(sections) else "detail"].append(claim)
    output: list[str] = []
    for heading, roles in sections:
        values = [claim for role in roles for claim in by_role.pop(role, ())]
        if not values:
            continue
        output.append(f"## {heading}")
        if kind == "procedure" and "step" in roles:
            output.extend(
                f"{ordinal}. {_claim_text(claim, chinese=chinese)}"
                for ordinal, claim in enumerate(values, 1)
            )
        else:
            output.extend(f"- {_claim_text(claim, chinese=chinese)}" for claim in values)
    leftovers = [claim for values in by_role.values() for claim in values]
    if leftovers:
        output.append("## 补充信息" if chinese else "## Details")
        output.extend(f"- {_claim_text(claim, chinese=chinese)}" for claim in leftovers)
    return "\n\n".join(output)


def _claim_text(claim: RenderedKnowledgeClaim, *, chinese: bool) -> str:
    scope = tuple(
        value
        for _dimension, value in claim.applicability
        if value and value != UNSPECIFIED_APPLICABILITY
    )
    unspecified = tuple(
        dimension for dimension, value in claim.applicability if value == UNSPECIFIED_APPLICABILITY
    )
    applicability = ""
    if scope or unspecified:
        label = "适用：" if chinese else "Applies to: "
        details = list(scope)
        if unspecified:
            dimensions = "、".join(unspecified) if chinese else ", ".join(unspecified)
            details.append(f"未指定：{dimensions}" if chinese else f"unspecified: {dimensions}")
        applicability = (
            f"（{label}{'；'.join(details)}）" if chinese else f" ({label}{'; '.join(details)})"
        )
    markers = "".join(claim.source_markers)
    return f"{claim.text}{applicability}{markers}"


def _known_roles(sections: tuple[tuple[str, tuple[str, ...]], ...]) -> frozenset[str]:
    return frozenset(role for _heading, roles in sections for role in roles)
