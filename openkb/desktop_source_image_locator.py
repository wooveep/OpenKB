"""Shared source-position matching for original images and EvidenceRefs."""

from __future__ import annotations

import re

_CELL_RANGE_PATTERN = re.compile(
    r"^\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$", re.IGNORECASE
)


def source_image_matches_evidence(
    source_image_id: str,
    image_locator: dict[str, object],
    evidence_locator: dict[str, object],
) -> bool:
    """Match an image to an exact source position, never document affinity alone."""
    if evidence_locator.get("source_image_id") == source_image_id:
        return True
    if _same_locator_value(image_locator, evidence_locator, "body_order"):
        return True
    if _same_locator_value(image_locator, evidence_locator, "sheet"):
        return _cell_ranges_overlap(
            image_locator.get("cell_range"), evidence_locator.get("cell_range")
        )
    if "page" in image_locator and "page" in evidence_locator:
        if not _same_locator_value(image_locator, evidence_locator, "page"):
            return False
        return _bbox_matches(image_locator, evidence_locator)
    if "slide" in image_locator and "slide" in evidence_locator:
        return False
    return _line_ranges_overlap(image_locator, evidence_locator)


def _same_locator_value(
    image_locator: dict[str, object], evidence_locator: dict[str, object], key: str
) -> bool:
    value = image_locator.get(key)
    return value is not None and value == evidence_locator.get(key)


def _bbox_matches(image_locator: dict[str, object], evidence_locator: dict[str, object]) -> bool:
    image_bbox = image_locator.get("bbox")
    evidence_bbox = evidence_locator.get("bbox")
    if not isinstance(image_bbox, list) or not isinstance(evidence_bbox, list):
        return False
    if len(image_bbox) != 4 or len(evidence_bbox) != 4:
        return False
    try:
        image_values = tuple(float(value) for value in image_bbox)
        evidence_values = tuple(float(value) for value in evidence_bbox)
    except (TypeError, ValueError):
        return False
    return not (
        image_values[2] <= evidence_values[0]
        or image_values[0] >= evidence_values[2]
        or image_values[3] <= evidence_values[1]
        or image_values[1] >= evidence_values[3]
    )


def _line_ranges_overlap(
    image_locator: dict[str, object], evidence_locator: dict[str, object]
) -> bool:
    image_start = image_locator.get("line_start")
    image_end = image_locator.get("line_end")
    evidence_start = evidence_locator.get("line_start")
    evidence_end = evidence_locator.get("line_end")
    if not (
        isinstance(image_start, int)
        and isinstance(image_end, int)
        and isinstance(evidence_start, int)
        and isinstance(evidence_end, int)
    ):
        return False
    return max(image_start, evidence_start) <= min(image_end, evidence_end)


def _cell_ranges_overlap(left: object, right: object) -> bool:
    left_bounds = _cell_range_bounds(left)
    right_bounds = _cell_range_bounds(right)
    if left_bounds is None or right_bounds is None:
        return False
    return not (
        left_bounds[2] < right_bounds[0]
        or left_bounds[0] > right_bounds[2]
        or left_bounds[3] < right_bounds[1]
        or left_bounds[1] > right_bounds[3]
    )


def _cell_range_bounds(value: object) -> tuple[int, int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _CELL_RANGE_PATTERN.match(value.strip())
    if match is None:
        return None
    start_column, start_row, end_column, end_row = match.groups()
    return (
        _column_number(start_column),
        int(start_row),
        _column_number(end_column or start_column),
        int(end_row or start_row),
    )


def _column_number(value: str) -> int:
    result = 0
    for character in value.upper():
        result = result * 26 + ord(character) - ord("A") + 1
    return result
