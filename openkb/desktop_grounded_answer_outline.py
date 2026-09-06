"""Domain-neutral citation postcondition for grounded-answer list claims."""

from __future__ import annotations

import re

_CITATION_PATTERN = re.compile(r"\[(\d+)\]")
_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")


def citation_guarded_answer(answer_text: str, *, evidence_count: int) -> str:
    """Drop list claims that cite none of the supplied Evidence entries.

    The guard deliberately does not classify headings, question kinds, or domain
    vocabulary. It enforces the same structural citation requirement for every
    prose list item and leaves non-list prose to the answer validator and review.
    """
    guarded: list[str] = []
    in_fence = False
    for line in answer_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            guarded.append(line)
            continue
        if (
            not in_fence
            and _LIST_ITEM_PATTERN.match(line)
            and not any(
                1 <= int(match) <= evidence_count for match in _CITATION_PATTERN.findall(line)
            )
        ):
            continue
        guarded.append(line)
    return "\n".join(guarded).strip()
