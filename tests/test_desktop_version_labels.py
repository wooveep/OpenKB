"""Table-driven version parsing and ordering boundaries."""

from __future__ import annotations

import pytest

from openkb.desktop_version_labels import (
    compare_version_labels,
    parse_version_label,
    version_label_candidates,
)


@pytest.mark.parametrize(
    ("scheme", "older", "newer"),
    (
        ("numeric_dotted", "V10.3", "V10.10"),
        ("semver", "1.2.0-rc.1", "1.2.0"),
        ("semver", "1.2.9", "1.10.0"),
        ("calendar", "2026-08-31", "2026-09-04"),
    ),
)
def test_version_schemes_use_structured_order(scheme, older, newer) -> None:
    assert compare_version_labels(older, newer, scheme) == -1
    assert compare_version_labels(newer, older, scheme) == 1


def test_opaque_versions_are_equal_or_incomparable() -> None:
    assert compare_version_labels("Release Blue", "release blue", "opaque") == 0
    assert compare_version_labels("Release Blue", "Release Green", "opaque") is None


@pytest.mark.parametrize(
    "text",
    (
        "Connect to 10.2.3.4 on port 8443.",
        "The threshold is 10.3 and the appliance is XG-10.2.",
        "Use build date fragment 2026-09 without a version signal.",
        "V2V migration keeps the source and destination virtual machines distinct.",
    ),
)
def test_false_positive_numbers_are_not_version_labels(text: str) -> None:
    assert version_label_candidates(text) == ()


def test_explicit_or_catalog_known_labels_are_candidates() -> None:
    assert version_label_candidates("Compare version V10.2 with V10.10") == (
        "10.2",
        "10.10",
    )
    assert version_label_candidates("What changed in 10.2?", known_labels=("V10.2",)) == ("V10.2",)
    assert parse_version_label("V10.10", "numeric_dotted") is not None
