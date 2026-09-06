"""Deterministic paragraph-level three-way merge for Knowledge Working Drafts."""

from __future__ import annotations

import difflib
import re

from openkb.importing.artifacts import DesktopImportError
from openkb.knowledge.pages.generations import normalized_knowledge_content
from openkb.knowledge.pages.sources import strip_knowledge_source_markers
from openkb.knowledge.pages.titles import normalize_knowledge_title

_FIELD_PATTERN = re.compile(r"^\s*(?:[-*+]\s*)?(?P<key>[^:：\n]{1,80})\s*[:：]\s*\S")
_MergeHunk = tuple[int, int, tuple[str, ...], str]


def apply_incoming_to_draft(*, baseline: str, draft: str, incoming: str) -> str:
    """Apply non-overlapping baseline-to-incoming edits to a Working Draft."""
    baseline_units = _markdown_units(baseline)
    draft_hunks = _edit_hunks(
        baseline_units,
        _markdown_units(draft),
        source="draft",
        ignore_source_markers=False,
    )
    incoming_hunks = _edit_hunks(
        baseline_units,
        _markdown_units(incoming),
        source="incoming",
        ignore_source_markers=True,
    )
    for draft_hunk in draft_hunks:
        for incoming_hunk in incoming_hunks:
            if _equivalent_hunks(draft_hunk, incoming_hunk):
                continue
            if _overlapping_hunks(draft_hunk, incoming_hunk):
                _raise_manual_merge_required()
    merged_hunks = list(draft_hunks)
    for incoming_hunk in incoming_hunks:
        if not any(_equivalent_hunks(incoming_hunk, value) for value in draft_hunks):
            merged_hunks.append(incoming_hunk)
    merged_hunks.sort(
        key=lambda value: (
            value[0],
            0 if value[0] == value[1] else 1,
            0 if value[3] == "draft" else 1,
        )
    )
    result: list[str] = []
    cursor = 0
    for start, end, replacement, _source in merged_hunks:
        if start < cursor:
            _raise_manual_merge_required()
        result.extend(baseline_units[cursor:start])
        result.extend(replacement)
        cursor = end
    result.extend(baseline_units[cursor:])
    return "\n\n".join(result)


def _edit_hunks(
    baseline: tuple[str, ...],
    changed: tuple[str, ...],
    *,
    source: str,
    ignore_source_markers: bool,
) -> tuple[_MergeHunk, ...]:
    baseline_keys = tuple(
        _merge_unit_identity(value, ignore_source_markers=ignore_source_markers)
        for value in baseline
    )
    changed_keys = tuple(
        _merge_unit_identity(value, ignore_source_markers=ignore_source_markers)
        for value in changed
    )
    matcher = difflib.SequenceMatcher(None, baseline_keys, changed_keys, autojunk=False)
    return tuple(
        (baseline_start, baseline_end, changed[changed_start:changed_end], source)
        for operation, baseline_start, baseline_end, changed_start, changed_end in (
            matcher.get_opcodes()
        )
        if operation != "equal"
    )


def _merge_unit_identity(value: str, *, ignore_source_markers: bool) -> str:
    comparable = strip_knowledge_source_markers(value) if ignore_source_markers else value
    return normalized_knowledge_content(comparable)


def _equivalent_hunks(left: _MergeHunk, right: _MergeHunk) -> bool:
    return left[:2] == right[:2] and tuple(
        _merge_unit_identity(value, ignore_source_markers=True) for value in left[2]
    ) == tuple(_merge_unit_identity(value, ignore_source_markers=True) for value in right[2])


def _overlapping_hunks(left: _MergeHunk, right: _MergeHunk) -> bool:
    left_start, left_end = left[:2]
    right_start, right_end = right[:2]
    if left_start == left_end and right_start == right_end:
        return bool(_replacement_field_keys(left[2]) & _replacement_field_keys(right[2]))
    if left_start == left_end:
        return right_start < left_start < right_end
    if right_start == right_end:
        return left_start < right_start < left_end
    return max(left_start, right_start) < min(left_end, right_end)


def _replacement_field_keys(values: tuple[str, ...]) -> frozenset[str]:
    keys: set[str] = set()
    for value in values:
        match = _FIELD_PATTERN.match(strip_knowledge_source_markers(value))
        if match is None:
            continue
        _, normalized = normalize_knowledge_title(str(match.group("key")))
        if normalized:
            keys.add(normalized)
    return frozenset(keys)


def _markdown_units(value: str) -> tuple[str, ...]:
    return tuple(unit.strip() for unit in value.split("\n\n") if unit.strip())


def _raise_manual_merge_required() -> None:
    raise DesktopImportError(
        "knowledge_reconciliation_manual_merge_required",
        "The Working Draft and incoming document changed the same content. "
        "Use manual merge to resolve it.",
    )
