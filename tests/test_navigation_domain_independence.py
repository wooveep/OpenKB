"""Navigation policy must be invariant to corpus- and benchmark-specific vocabulary."""

from __future__ import annotations

import inspect
from pathlib import Path

from openkb.desktop_adaptive_navigation import initial_navigation_objective
from openkb.desktop_answer_types import DesktopEvidenceRef, DesktopRetrievalPlan
from openkb.desktop_knowledge_navigation_windows import phase_diverse_source_window
from openkb.desktop_navigation_evidence import allocate_evidence

_PRODUCTION_POLICY_FILES = (
    "desktop_adaptive_navigation.py",
    "desktop_knowledge_navigation.py",
    "desktop_knowledge_navigation_routes.py",
    "desktop_knowledge_navigation_windows.py",
    "desktop_navigation_evidence.py",
    "desktop_navigation_ranking.py",
    "desktop_prompt_contracts.py",
)
_FIXTURE_VOCABULARY = (
    "ocloudservicetool",
    "mariadb",
    "mysql-bin",
    "show master status",
    "change master to",
    "start slave",
    "stop slave",
    "reset slave",
    "grant replication",
    "bcache",
    "gluster",
    "双节点",
    "超融合",
    "ntp",
    "resource-pool",
    "扩容",
    "缩容",
    "运维",
)


def _reference(evidence_id: str, section: str, excerpt: str) -> DesktopEvidenceRef:
    return DesktopEvidenceRef(
        evidence_id=evidence_id,
        document_id="guide",
        document_name="Guide",
        section=section,
        locator={},
        excerpt=excerpt,
        channels=("knowledge_navigation_source_window",),
    )


def test_navigation_policy_does_not_embed_fixture_vocabulary() -> None:
    package = Path(__file__).parents[1] / "openkb"

    occurrences = {
        token: filename
        for filename in _PRODUCTION_POLICY_FILES
        for token in _FIXTURE_VOCABULARY
        if token in (package / filename).read_text(encoding="utf-8").casefold()
    }

    assert occurrences == {}


def test_source_window_order_is_invariant_to_excerpt_vocabulary() -> None:
    identifiers = tuple(f"block-{ordinal}" for ordinal in range(12))
    fixture_excerpts = (
        "Background zero.",
        "Background one.",
        "Background two.",
        "Background three.",
        "Background four.",
        "Background five.",
        "GRANT REPLICATION SLAVE ON *.* TO repl@'host';",
        "show master status;",
        "CHANGE MASTER TO MASTER_HOST='host';",
        "start slave;",
        "show slave status\\G;",
        "Background eleven.",
    )
    renamed_excerpts = tuple(f"Opaque content {ordinal}." for ordinal in range(12))

    fixture_order = tuple(
        item.evidence_id
        for item in phase_diverse_source_window(
            tuple(
                _reference(evidence_id, "Guide / One section", excerpt)
                for evidence_id, excerpt in zip(identifiers, fixture_excerpts, strict=True)
            )
        )
    )
    renamed_order = tuple(
        item.evidence_id
        for item in phase_diverse_source_window(
            tuple(
                _reference(evidence_id, "Guide / One section", excerpt)
                for evidence_id, excerpt in zip(identifiers, renamed_excerpts, strict=True)
            )
        )
    )

    assert fixture_order == renamed_order


def test_source_window_order_is_invariant_to_section_vocabulary() -> None:
    fixture_sections = ("Guide / Expansion", "Guide / Core", "Guide / Recovery")
    renamed_sections = ("Guide / Amber", "Guide / Core", "Guide / Violet")

    fixture_order = tuple(
        item.evidence_id
        for item in phase_diverse_source_window(
            tuple(
                _reference(f"phase-{ordinal}", section, f"Detail {ordinal}.")
                for ordinal, section in enumerate(fixture_sections)
            )
        )
    )
    renamed_order = tuple(
        item.evidence_id
        for item in phase_diverse_source_window(
            tuple(
                _reference(f"phase-{ordinal}", section, f"Detail {ordinal}.")
                for ordinal, section in enumerate(renamed_sections)
            )
        )
    )

    assert fixture_order == renamed_order


def test_seed_objective_does_not_guess_actions_or_constraints_from_domain_words() -> None:
    plan = DesktopRetrievalPlan(
        query="如何在双节点集群安装 Nebula",
        terms=("Nebula", "双节点", "安装"),
        source="model",
    )

    objective = initial_navigation_objective(plan.query, plan)

    assert objective.answer_kind == "how_to"
    assert objective.user_actions == ()
    assert objective.constraints == ()


def test_evidence_allocation_interface_has_no_query_vocabulary_hook() -> None:
    assert "terms" not in inspect.signature(allocate_evidence).parameters
