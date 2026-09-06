"""Bounded Document IR extraction for Knowledge Reconciliation."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from openkb.importing.artifacts import DocumentIRBlock
from openkb.knowledge.pages.generations import (
    KnowledgeGenerationSource,
    knowledge_content_sha256,
    normalized_knowledge_content,
)
from openkb.knowledge.pages.sources import strip_knowledge_source_markers
from openkb.knowledge.pages.titles import normalize_knowledge_title

_MAX_CANDIDATES_PER_DOCUMENT = 32
_MAX_CANDIDATE_CHARACTERS = 24_000
_KIND_PREFIX = re.compile(
    r"^\s*(?:(?P<english>concept|entity|procedure)|"
    r"(?P<chinese>概念|实体|流程|过程|操作))\s*[:：]\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_FIELD_PATTERN = re.compile(r"^\s*(?:[-*+]\s*)?(?P<key>[^:：\n]{1,80})\s*[:：]\s*\S")


@dataclass(frozen=True)
class IncomingKnowledgeChange:
    """One bounded knowledge candidate extracted from Document IR."""

    source_block_id: str | None
    kind: str
    is_kind_explicit: bool
    title: str
    normalized_title: str
    content_markdown: str
    content_sha256: str
    aliases: tuple[str, ...] = ()
    identity_labels: tuple[str, ...] = ()
    sources: tuple[KnowledgeGenerationSource, ...] = ()
    analysis_provenance_json: str | None = None


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


def knowledge_relationship(incoming: str, baseline: str) -> str:
    """Classify one normalized incoming value against a published baseline."""
    incoming_content = normalized_knowledge_content(strip_knowledge_source_markers(incoming))
    baseline_content = normalized_knowledge_content(strip_knowledge_source_markers(baseline))
    if incoming_content == baseline_content:
        return "duplicate"
    incoming_units = frozenset(part for part in incoming_content.split("\n") if part)
    baseline_units = frozenset(part for part in baseline_content.split("\n") if part)
    if incoming_units and incoming_units <= baseline_units:
        return "duplicate"
    if _is_compatible_structured_addition(incoming_units, baseline_units):
        return "compatible_addition"
    return "conflict"


def _is_compatible_structured_addition(
    incoming_units: frozenset[str], baseline_units: frozenset[str]
) -> bool:
    if not baseline_units or not baseline_units < incoming_units:
        return False
    baseline_fields = tuple(_field_key(unit) for unit in baseline_units)
    additional_fields = tuple(_field_key(unit) for unit in incoming_units - baseline_units)
    if any(field is None for field in (*baseline_fields, *additional_fields)):
        return False
    known_fields = tuple(str(field) for field in baseline_fields)
    added_fields = tuple(str(field) for field in additional_fields)
    return (
        len(set(known_fields)) == len(known_fields)
        and len(set(added_fields)) == len(added_fields)
        and all(field not in known_fields for field in added_fields)
    )


def _field_key(value: str) -> str | None:
    match = _FIELD_PATTERN.match(value)
    if match is None:
        return None
    key, _ = normalize_knowledge_title(str(match.group("key")))
    return key or None


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
    explicit_kind = english.casefold() if english is not None else None
    if explicit_kind == "entity" or chinese == "实体":
        kind = "entity"
    elif explicit_kind == "procedure" or chinese in {"流程", "过程", "操作"}:
        kind = "procedure"
    else:
        kind = "concept"
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
