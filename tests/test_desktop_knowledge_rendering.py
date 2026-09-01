"""Readable, evidence-bound rendering for generated knowledge pages."""

from __future__ import annotations

from openkb.desktop_knowledge_rendering import (
    RenderedKnowledgeClaim,
    render_generated_knowledge,
)


def _claim(text: str, role: str, marker: str) -> RenderedKnowledgeClaim:
    return RenderedKnowledgeClaim(text=text, role=role, source_markers=(marker,))


def test_concept_sections_integrate_claims_as_source_bound_prose() -> None:
    rendered = render_generated_knowledge(
        "concept",
        (
            _claim("双节点超融合由两台节点组成。", "definition", "[^s1]"),
            _claim("两台节点共同承载计算与存储。", "purpose", "[^s2]"),
            _claim("存储使用双副本。", "mechanism", "[^s3]"),
        ),
        language="zh",
    )

    assert (
        "## 定义与说明\n\n双节点超融合由两台节点组成。[^s1] 两台节点共同承载计算与存储。[^s2]"
    ) in rendered
    assert "## 机制与能力\n\n存储使用双副本。[^s3]" in rendered
    assert "\n\n- " not in rendered


def test_procedure_keeps_operational_lists_and_ordered_steps() -> None:
    rendered = render_generated_knowledge(
        "procedure",
        (
            _claim("部署双节点超融合环境。", "purpose", "[^s1]"),
            _claim("准备两台服务器。", "prerequisite", "[^s2]"),
            _claim("安装系统。", "step", "[^s3]"),
            _claim("创建双副本卷。", "step", "[^s4]"),
            _claim("使用 VIP 登录。", "validation", "[^s5]"),
        ),
        language="zh",
    )

    assert "## 目标\n\n部署双节点超融合环境。[^s1]" in rendered
    assert "## 前置条件\n\n- 准备两台服务器。[^s2]" in rendered
    assert "## 操作步骤\n\n1. 安装系统。[^s3]\n\n2. 创建双副本卷。[^s4]" in rendered
    assert "## 验证\n\n- 使用 VIP 登录。[^s5]" in rendered
