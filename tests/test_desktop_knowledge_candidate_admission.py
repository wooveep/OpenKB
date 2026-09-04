"""Code-owned candidate admission policy behavior."""

from __future__ import annotations

import pytest

from openkb.desktop_knowledge_candidate_admission import assess_knowledge_candidate


def test_entity_subtype_cannot_bypass_independent_description() -> None:
    decision = assess_knowledge_candidate(
        kind="entity",
        title="Alpha",
        subtype="service",
        claims=(("detail", "This section configures a timeout value."),),
    )

    assert not decision.admitted
    assert decision.reason == "entity_not_independently_described"


def test_new_entity_analysis_uses_the_code_owned_subtype_ontology() -> None:
    decision = assess_knowledge_candidate(
        kind="entity",
        title="Alpha",
        subtype="whatever-the-model-invented",
        claims=(("definition", "Alpha is a durable named service."),),
    )

    assert not decision.admitted
    assert decision.reason == "unsupported_entity_subtype"


@pytest.mark.parametrize(
    "literal",
    (
        "Teacher.deb",
        "agent-2.1.rpm",
        "connector.jar",
        "snapshot.dat",
        "configs/service",
        "admin@example.test",
        "timeout=30",
    ),
)
def test_file_package_path_account_and_config_literals_never_become_entities(
    literal: str,
) -> None:
    decision = assess_knowledge_candidate(
        kind="entity",
        title=literal,
        subtype="product",
        claims=(("definition", f"{literal} is described as a durable product."),),
    )

    assert not decision.admitted
    assert decision.reason == "raw_literal"


def test_other_named_entity_is_not_an_unreviewed_escape_hatch() -> None:
    decision = assess_knowledge_candidate(
        kind="entity",
        title="Alpha",
        subtype="other_named_entity",
        claims=(("definition", "Alpha is a durable named offering."),),
    )

    assert not decision.admitted
    assert decision.reason == "other_named_entity_requires_review_reason"


@pytest.mark.parametrize(
    "title",
    (
        "Alpha depends on Beta",
        "Alpha is part of Beta",
        "Alpha 依赖于 Beta",
        "Alpha 属于 Beta",
    ),
)
def test_relation_phrases_are_not_promoted_to_entities(title: str) -> None:
    decision = assess_knowledge_candidate(
        kind="entity",
        title=title,
        subtype="named_system",
        claims=(("definition", f"{title} is described by this relation."),),
    )

    assert not decision.admitted
    assert decision.reason == "relation_phrase"


def test_unsupported_candidate_kind_fails_closed() -> None:
    decision = assess_knowledge_candidate(
        kind="heading",
        title="Deployment",
        subtype=None,
        claims=(("detail", "Deployment describes the installation section."),),
    )

    assert not decision.admitted
    assert decision.reason == "unsupported_kind"
