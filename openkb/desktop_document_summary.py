"""Deterministic whole-document summary coverage bounds."""

from __future__ import annotations

from typing import TypeVar

SummaryUnitT = TypeVar("SummaryUnitT")
DEFAULT_DOCUMENT_SUMMARY_UNIT_LIMIT = 32


def bounded_document_summary_units(
    units: tuple[SummaryUnitT, ...],
    *,
    maximum: int = DEFAULT_DOCUMENT_SUMMARY_UNIT_LIMIT,
) -> tuple[SummaryUnitT, ...]:
    """Sample the whole ordered document without favoring code-named semantic roles."""
    if maximum < 0:
        raise ValueError("Document Summary maximum must not be negative.")
    if len(units) <= maximum:
        return units
    if maximum == 0:
        return ()
    if maximum == 1:
        return (units[len(units) // 2],)
    indexes = {round(ordinal * (len(units) - 1) / (maximum - 1)) for ordinal in range(maximum)}
    return tuple(units[index] for index in sorted(indexes))
