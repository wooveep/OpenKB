"""Metamorphic checks for deterministic knowledge-title match signals."""

from __future__ import annotations

from openkb.knowledge.pages.titles import (
    controlled_latin_title_key,
    normalize_knowledge_title,
)


def test_title_normalization_applies_nfkc_casefold_and_common_punctuation() -> None:
    display, lookup = normalize_knowledge_title("  ＯＣｌｏｕｄ—Ｖｉｅｗ： Console  ")

    assert display == "OCloud-View: Console"
    assert lookup == "ocloud-view: console"


def test_controlled_latin_separator_variants_produce_one_match_signal() -> None:
    variants = ("OCloudView", "OCloud View", "OCloud-View", "ＯＣｌｏｕｄ＿Ｖｉｅｗ")

    assert {controlled_latin_title_key(value) for value in variants} == {"ocloudview"}


def test_controlled_latin_signal_does_not_delete_semantic_chinese_suffixes() -> None:
    assert controlled_latin_title_key("OCloudView 管理平台") != controlled_latin_title_key(
        "OCloudView 云桌面管理平台"
    )


def test_shared_latin_prefix_is_not_identity_equivalence() -> None:
    assert controlled_latin_title_key("Cloud View") != controlled_latin_title_key("Cloud Viewer")
