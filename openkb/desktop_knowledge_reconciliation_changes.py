"""Bounded Document IR extraction for Knowledge Reconciliation."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_knowledge_generations import knowledge_content_sha256
from openkb.desktop_knowledge_titles import normalize_knowledge_title

_MAX_CANDIDATES_PER_DOCUMENT = 32
_MAX_CANDIDATE_CHARACTERS = 24_000
_KIND_PREFIX = re.compile(
    r"^\s*(?:(?P<english>concept|entity)|(?P<chinese>概念|实体))\s*[:：]\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IncomingKnowledgeChange:
    """One bounded Concept or Entity candidate extracted from Document IR."""

    source_block_id: str | None
    kind: str
    is_kind_explicit: bool
    title: str
    normalized_title: str
    content_markdown: str
    content_sha256: str


def extract_incoming_knowledge_changes(
    blocks: tuple[DocumentIRBlock, ...], document_name: str
) -> tuple[IncomingKnowledgeChange, ...]:
    """Extract deterministic section candidates without publishing them."""
    headings = tuple(
        (index, block) for index, block in enumerate(blocks) if block.kind == "heading"
    )
    candidates: list[tuple[str | None, str, bool, str, str]] = []
    if headings:
        for position, (index, heading) in enumerate(headings[:_MAX_CANDIDATES_PER_DOCUMENT]):
            end = headings[position + 1][0] if position + 1 < len(headings) else len(blocks)
            body = _bounded_content(
                block.text for block in blocks[index + 1 : end] if block.kind != "heading"
            )
            if body:
                kind, title, is_kind_explicit = _kind_and_title(heading.text)
                candidates.append((heading.block_id, kind, is_kind_explicit, title, body))
    else:
        body = _bounded_content(block.text for block in blocks)
        if body:
            candidates.append((None, "concept", False, Path(document_name).stem, body))
    return _merge_changes(candidates)


def _merge_changes(
    values: list[tuple[str | None, str, bool, str, str]],
) -> tuple[IncomingKnowledgeChange, ...]:
    merged: dict[tuple[str, str, bool], tuple[str | None, str, bool, str, list[str]]] = {}
    for source_block_id, kind, is_kind_explicit, untrusted_title, content in values:
        title, normalized_title = normalize_knowledge_title(untrusted_title)
        if not title:
            continue
        key = kind, normalized_title, is_kind_explicit
        if key not in merged:
            merged[key] = (source_block_id, kind, is_kind_explicit, title, [content])
        elif content not in merged[key][4]:
            merged[key][4].append(content)
    changes: list[IncomingKnowledgeChange] = []
    for source_block_id, kind, is_kind_explicit, title, contents in merged.values():
        content_markdown = _bounded_content(contents)
        if not content_markdown:
            continue
        _, normalized_title = normalize_knowledge_title(title)
        changes.append(
            IncomingKnowledgeChange(
                source_block_id=source_block_id,
                kind=kind,
                is_kind_explicit=is_kind_explicit,
                title=title,
                normalized_title=normalized_title,
                content_markdown=content_markdown,
                content_sha256=knowledge_content_sha256(content_markdown),
            )
        )
    return tuple(changes)


def _kind_and_title(value: str) -> tuple[str, str, bool]:
    match = _KIND_PREFIX.match(value)
    if match is None:
        return "concept", value, False
    english = match.group("english")
    chinese = match.group("chinese")
    kind = (
        "entity"
        if (english is not None and english.casefold() == "entity") or chinese == "实体"
        else "concept"
    )
    return kind, str(match.group("title")), True


def _bounded_content(values: Iterable[object]) -> str:
    parts: list[str] = []
    remaining = _MAX_CANDIDATE_CHARACTERS
    for value in values:
        if not isinstance(value, str):
            continue
        content = value.strip()
        if not content:
            continue
        content = content[:remaining]
        parts.append(content)
        remaining -= len(content)
        if remaining <= 0:
            break
    return "\n\n".join(parts)
