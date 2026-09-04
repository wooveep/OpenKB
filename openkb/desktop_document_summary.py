"""Deterministic whole-document summary coverage bounds."""

from __future__ import annotations

from typing import Protocol, TypeVar


class SummaryUnit(Protocol):
    @property
    def role(self) -> str: ...


SummaryUnitT = TypeVar("SummaryUnitT", bound=SummaryUnit)
DEFAULT_DOCUMENT_SUMMARY_UNIT_LIMIT = 32


def bounded_document_summary_units(
    units: tuple[SummaryUnitT, ...],
    *,
    maximum: int = DEFAULT_DOCUMENT_SUMMARY_UNIT_LIMIT,
) -> tuple[SummaryUnitT, ...]:
    """Retain role and whole-document coverage under the published bound."""
    if maximum < 0:
        raise ValueError("Document Summary maximum must not be negative.")
    if len(units) <= maximum:
        return units
    if maximum == 0:
        return ()
    selected: set[int] = set()
    for role in ("purpose", "applicability", "key_topic"):
        match = next((index for index, unit in enumerate(units) if unit.role == role), None)
        if match is not None and len(selected) < maximum:
            selected.add(match)
    coverage_slots = maximum - len(selected)
    if coverage_slots == 1:
        selected.add(len(units) // 2)
    elif coverage_slots > 1:
        for ordinal in range(coverage_slots):
            selected.add(round(ordinal * (len(units) - 1) / (coverage_slots - 1)))
    if len(selected) < maximum:
        for index in range(len(units)):
            selected.add(index)
            if len(selected) == maximum:
                break
    return tuple(units[index] for index in sorted(selected))
