"""Domain-neutral rendering primitives for evidence-bound claims."""

from __future__ import annotations

from openkb.knowledge.pages.rendering import (
    RenderedKnowledgeClaim,
    render_generated_knowledge,
)


def _claim(
    text: str,
    marker: str,
    applicability: tuple[tuple[str, str], ...] = (),
) -> RenderedKnowledgeClaim:
    return RenderedKnowledgeClaim(
        text=text,
        source_markers=(marker,),
        applicability=applicability,
    )


def test_claims_render_without_kind_language_or_role_derived_sections() -> None:
    claims = (
        _claim("双节点超融合由两台节点组成。", "[^s1]"),
        _claim("存储使用双副本。", "[^s2]"),
    )

    concept = render_generated_knowledge("concept", claims, language="zh")
    procedure = render_generated_knowledge("procedure", claims, language="en")

    assert concept == procedure
    assert concept == "双节点超融合由两台节点组成。[^s1]\n\n存储使用双副本。[^s2]"
    assert "##" not in concept
    assert "\n- " not in concept


def test_dynamic_applicability_is_escaped_and_keeps_its_source_marker() -> None:
    rendered = render_generated_knowledge(
        "entity",
        (
            _claim(
                "A < B uses [notation].",
                "[^source]",
                (("范围", "实验 | 对照"),),
            ),
        ),
    )

    assert rendered == ("A \\< B uses \\[notation\\]. (范围: 实验 \\| 对照)[^source]")
