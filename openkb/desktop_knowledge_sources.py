"""Claim-level source binding for Desktop Knowledge Page revisions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

_SOURCE_MARKER = re.compile(r"\[\^(src-[^\]\s]+)\](?!:)")
_MARKDOWN_LINK = re.compile(r"!?\[([^]]*)\]\(([^)]+)\)")
_TERM_PATTERN = re.compile(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]+")
_SOURCE_SEARCH_LIMIT = 20
_SOURCE_SEARCH_QUERY_LIMIT = 500
_SOURCE_SEARCH_TERM_LIMIT = 24

AVAILABLE_EVIDENCE_OCCURRENCES_CTE = """
WITH available_evidence_occurrences AS (
    SELECT evidence_occurrences.evidence_id, evidence_occurrences.document_id,
        source_documents.display_name, document_ir_blocks.heading_path,
        document_ir_blocks.locator_json, evidence_refs.text, evidence_occurrences.ordinal,
        ROW_NUMBER() OVER (
            PARTITION BY evidence_occurrences.evidence_id
            ORDER BY source_documents.created_at, source_documents.document_id,
                evidence_occurrences.ordinal
        ) AS occurrence_rank
    FROM evidence_occurrences
    JOIN evidence_refs ON evidence_refs.evidence_id = evidence_occurrences.evidence_id
    JOIN source_documents ON source_documents.document_id = evidence_occurrences.document_id
    JOIN document_ir_blocks ON document_ir_blocks.block_id = evidence_occurrences.block_id
    WHERE source_documents.availability = 'available'
)
"""


@dataclass(frozen=True)
class DesktopKnowledgeSourceCandidate:
    evidence_id: str
    document_id: str
    document_name: str
    section: str
    locator: dict[str, object]
    excerpt: str

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "section": self.section,
            "locator": self.locator,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class DesktopKnowledgeSourceMapEntry(DesktopKnowledgeSourceCandidate):
    source_id: str
    claim_text: str
    availability: Literal["available", "unavailable"]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "claim_text": self.claim_text,
            "availability": self.availability,
            **super().as_dict(),
        }


@dataclass(frozen=True)
class DesktopKnowledgePublicationDiagnostic:
    code: str
    message: str
    source_id: str

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "source_id": self.source_id}


def search_available_sources_in(
    connection: sqlite3.Connection, query: str
) -> tuple[DesktopKnowledgeSourceCandidate, ...]:
    """Search canonical Available Evidence by document, section, or original text."""
    terms = _source_search_terms(query)
    if not terms:
        return ()
    score_parts: list[str] = []
    parameters: list[object] = []
    for term in terms:
        score_parts.extend(
            (
                "CASE WHEN instr(lower(display_name), ?) > 0 THEN 4 ELSE 0 END",
                "CASE WHEN instr(lower(heading_path), ?) > 0 THEN 3 ELSE 0 END",
                "CASE WHEN instr(lower(text), ?) > 0 THEN 1 ELSE 0 END",
            )
        )
        parameters.extend((term, term, term))
    score_expression = " + ".join(score_parts)
    rows = connection.execute(
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        , scored_evidence_occurrences AS (
            SELECT evidence_id, document_id, display_name, heading_path, locator_json, text,
                ordinal, occurrence_rank, ({score_expression}) AS match_score
            FROM available_evidence_occurrences
        ), ranked_matching_occurrences AS (
            SELECT evidence_id, document_id, display_name, heading_path, locator_json, text,
                ordinal, match_score,
                ROW_NUMBER() OVER (
                    PARTITION BY evidence_id
                    ORDER BY match_score DESC, occurrence_rank
                ) AS match_rank
            FROM scored_evidence_occurrences
            WHERE match_score > 0
        )
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM ranked_matching_occurrences
        WHERE match_rank = 1
        ORDER BY match_score DESC, document_id, ordinal
        LIMIT ?
        """,
        (*parameters, _SOURCE_SEARCH_LIMIT),
    ).fetchall()
    return tuple(_candidate_from_row(row) for row in rows)


def available_source_in(
    connection: sqlite3.Connection, evidence_id: str
) -> DesktopKnowledgeSourceCandidate:
    """Resolve one canonical Evidence ID through a currently Available occurrence."""
    return _available_source_in(connection, evidence_id)


def bind_source_in(
    connection: sqlite3.Connection,
    *,
    page_id: str,
    claim_text: str,
    evidence_id: str,
    updated_at: str,
) -> None:
    """Insert one marker and Source Map row in the same Draft transaction."""
    row = connection.execute(
        "SELECT content_markdown FROM knowledge_page_working_drafts WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    if row is None:
        raise ValueError("knowledge_page_draft_not_found")
    content = str(row[0])
    claim = claim_text.strip()
    _require_claim_selection(content, claim)
    source = _available_source_in(connection, evidence_id)
    source_id = stable_source_id(evidence_id)
    marker = f"[^{source_id}]"
    if not knowledge_source_matches_claim(content, source_id, claim):
        content = content.replace(claim, f"{claim}{marker}", 1)
    connection.execute(
        """
        UPDATE knowledge_page_working_drafts
        SET content_markdown = ?, updated_at = ? WHERE page_id = ?
        """,
        (content, updated_at, page_id),
    )
    connection.execute(
        """
        INSERT INTO knowledge_page_working_sources (
            page_id, source_id, evidence_id, claim_text, document_id, document_name,
            section, locator_json, excerpt, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id, source_id, claim_text) DO UPDATE SET
            document_id = excluded.document_id,
            document_name = excluded.document_name,
            section = excluded.section,
            locator_json = excluded.locator_json,
            excerpt = excluded.excerpt
        """,
        (
            page_id,
            source_id,
            evidence_id,
            claim,
            source.document_id,
            source.document_name,
            source.section,
            json.dumps(source.locator, ensure_ascii=False, sort_keys=True),
            source.excerpt,
            updated_at,
        ),
    )


def copy_revision_sources_to_draft_in(
    connection: sqlite3.Connection, page_id: str, created_at: str
) -> None:
    """Start an edited Draft with the current revision's Source Map."""
    connection.execute(
        """
        INSERT INTO knowledge_page_working_sources (
            page_id, source_id, evidence_id, claim_text, document_id, document_name,
            section, locator_json, excerpt, created_at
        )
        SELECT ?, sources.source_id, sources.evidence_id, sources.claim_text,
            sources.document_id, sources.document_name, sources.section,
            sources.locator_json, sources.excerpt, ?
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revision_sources AS sources
            ON sources.revision_id = pages.current_revision_id
        WHERE pages.page_id = ?
        """,
        (page_id, created_at, page_id),
    )


def publish_draft_sources_in(
    connection: sqlite3.Connection, page_id: str, revision_id: str, created_at: str
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_page_revision_sources (
            revision_id, source_id, evidence_id, claim_text, document_id, document_name,
            section, locator_json, excerpt, created_at
        )
        SELECT ?, source_id, evidence_id, claim_text, document_id, document_name,
            section, locator_json, excerpt, ?
        FROM knowledge_page_working_sources WHERE page_id = ?
        """,
        (revision_id, created_at, page_id),
    )


def draft_source_map_in(
    connection: sqlite3.Connection, page_id: str
) -> tuple[DesktopKnowledgeSourceMapEntry, ...]:
    return _source_map_rows(
        connection,
        connection.execute(
            """
            SELECT source_id, evidence_id, claim_text, document_id, document_name,
                section, locator_json, excerpt
            FROM knowledge_page_working_sources
            WHERE page_id = ? ORDER BY source_id
            """,
            (page_id,),
        ).fetchall(),
    )


def revision_source_map_in(
    connection: sqlite3.Connection, revision_id: str | None
) -> tuple[DesktopKnowledgeSourceMapEntry, ...]:
    if revision_id is None:
        return ()
    return _source_map_rows(
        connection,
        connection.execute(
            """
            SELECT source_id, evidence_id, claim_text, document_id, document_name,
                section, locator_json, excerpt
            FROM knowledge_page_revision_sources
            WHERE revision_id = ? ORDER BY source_id
            """,
            (revision_id,),
        ).fetchall(),
    )


def publication_diagnostics_in(
    connection: sqlite3.Connection,
    content_markdown: str,
    source_map: tuple[DesktopKnowledgeSourceMapEntry, ...],
) -> tuple[DesktopKnowledgePublicationDiagnostic, ...]:
    marker_occurrences = _SOURCE_MARKER.findall(content_markdown)
    markers = set(marker_occurrences)
    mapped = {source.source_id for source in source_map}
    sources_by_id = {
        source_id: tuple(source for source in source_map if source.source_id == source_id)
        for source_id in mapped
    }
    claim_units = _claim_units(content_markdown)
    diagnostics: list[DesktopKnowledgePublicationDiagnostic] = []
    for source_id in sorted(mapped - markers):
        diagnostics.append(
            DesktopKnowledgePublicationDiagnostic(
                "knowledge_source_marker_missing",
                "Restore the source marker in the claim or remove its source binding.",
                source_id,
            )
        )
    for source_id in sorted(markers - mapped):
        diagnostics.append(
            DesktopKnowledgePublicationDiagnostic(
                "knowledge_source_unresolved",
                "Bind this source marker to Available Knowledge before publishing.",
                source_id,
            )
        )
    available_ids = _available_evidence_ids_in(
        connection, tuple(source.evidence_id for source in source_map)
    )
    for source in source_map:
        if source.evidence_id not in available_ids:
            diagnostics.append(
                DesktopKnowledgePublicationDiagnostic(
                    "knowledge_source_unavailable",
                    "The bound evidence is unavailable; choose another Available source.",
                    source.source_id,
                )
            )
    for claim in claim_units:
        claim_markers = set(_SOURCE_MARKER.findall(claim))
        claim_body = _normalized_claim_text(_SOURCE_MARKER.sub("", claim))
        if not claim_markers:
            claim_id = hashlib.sha256(claim_body.encode("utf-8")).hexdigest()[:16]
            diagnostics.append(
                DesktopKnowledgePublicationDiagnostic(
                    "knowledge_claim_source_missing",
                    "Bind this factual claim to Available Knowledge before publishing.",
                    f"claim-{claim_id}",
                )
            )
            continue
    for source_id in sorted(mapped & markers):
        matching_claims = [
            claim for claim in claim_units if source_id in _SOURCE_MARKER.findall(claim)
        ]
        expected = sorted(
            _normalized_claim_text(source.claim_text) for source in sources_by_id[source_id]
        )
        actual = sorted(
            _normalized_claim_text(_SOURCE_MARKER.sub("", claim)) for claim in matching_claims
        )
        if marker_occurrences.count(source_id) != len(actual) or actual != expected:
            diagnostics.append(
                DesktopKnowledgePublicationDiagnostic(
                    "knowledge_source_claim_mismatch",
                    "Rebind this source because the factual claim text changed.",
                    source_id,
                )
            )
    return tuple(diagnostics)


def prune_obsolete_draft_sources_in(
    connection: sqlite3.Connection,
    page_id: str,
    content_markdown: str,
    *,
    previous_content_markdown: str | None = None,
) -> None:
    """Drop bindings that no longer describe a claim in replacement Draft content."""
    rows = connection.execute(
        """
        SELECT source_id, claim_text
        FROM knowledge_page_working_sources
        WHERE page_id = ?
        """,
        (page_id,),
    ).fetchall()
    if not rows:
        return
    obsolete: list[tuple[str, str, str]] = []
    for source_id_value, claim_text_value in rows:
        source_id = str(source_id_value)
        claim_text = str(claim_text_value)
        was_valid = previous_content_markdown is None or knowledge_source_matches_claim(
            previous_content_markdown, source_id, claim_text
        )
        if was_valid and not knowledge_source_matches_claim(
            content_markdown, source_id, claim_text
        ):
            obsolete.append((page_id, source_id, claim_text))
    connection.executemany(
        """
        DELETE FROM knowledge_page_working_sources
        WHERE page_id = ? AND source_id = ? AND claim_text = ?
        """,
        obsolete,
    )


def strip_knowledge_source_markers(value: str) -> str:
    """Return knowledge Markdown without internal Source Map reference markers."""
    return _SOURCE_MARKER.sub("", value)


def merge_claim_source_markers(content_markdown: str, sources: tuple[object, ...]) -> str:
    """Attach new stable markers to their exact factual claim units."""
    parts = content_markdown.split("\n\n")
    for source in sources:
        source_id = str(getattr(source, "source_id"))
        marker = f"[^{source_id}]"
        expected = _normalized_claim_text(str(getattr(source, "claim_text")))
        matches = [
            index
            for index, part in enumerate(parts)
            if _normalized_claim_text(strip_knowledge_source_markers(part)) == expected
        ]
        if len(matches) == 1 and marker not in parts[matches[0]]:
            parts[matches[0]] = f"{parts[matches[0]]}{marker}"
    return "\n\n".join(parts)


def knowledge_source_matches_claim(content_markdown: str, source_id: str, claim_text: str) -> bool:
    """Return whether one exact factual claim retains its stable source marker."""
    matching_claims = [
        claim
        for claim in _claim_units(content_markdown)
        if source_id in _SOURCE_MARKER.findall(claim)
    ]
    expected = _normalized_claim_text(claim_text)
    return any(
        _normalized_claim_text(strip_knowledge_source_markers(claim)) == expected
        for claim in matching_claims
    )


def stable_source_id(evidence_id: str) -> str:
    return f"src-{hashlib.sha256(evidence_id.encode('utf-8')).hexdigest()[:16]}"


def validate_unbound_source_claim(claim_text: str) -> None:
    """Require one factual claim without OpenKB-owned source-marker syntax."""
    claim = claim_text.strip()
    if _SOURCE_MARKER.search(claim):
        raise ValueError("knowledge_source_marker_reserved")
    _require_claim_selection(claim, claim)


def validate_source_claim_selection(content_markdown: str, claim_text: str) -> None:
    """Require one exact factual claim unit inside editable Markdown."""
    _require_claim_selection(content_markdown, claim_text.strip())


def _available_source_in(
    connection: sqlite3.Connection, evidence_id: str
) -> DesktopKnowledgeSourceCandidate:
    sources = _available_sources_in(connection, (evidence_id,))
    if evidence_id not in sources:
        raise ValueError("knowledge_source_unavailable")
    return sources[evidence_id]


def _available_sources_in(
    connection: sqlite3.Connection, evidence_ids: tuple[str, ...]
) -> dict[str, DesktopKnowledgeSourceCandidate]:
    if not evidence_ids:
        return {}
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = connection.execute(
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences WHERE occurrence_rank = 1
            AND evidence_id IN ({placeholders})
        """,
        evidence_ids,
    ).fetchall()
    return {
        candidate.evidence_id: candidate for candidate in (_candidate_from_row(row) for row in rows)
    }


def _available_evidence_ids_in(
    connection: sqlite3.Connection, evidence_ids: tuple[str, ...]
) -> set[str]:
    if not evidence_ids:
        return set()
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = connection.execute(
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id FROM available_evidence_occurrences
        WHERE occurrence_rank = 1 AND evidence_id IN ({placeholders})
        """,
        evidence_ids,
    ).fetchall()
    return {str(row[0]) for row in rows}


def _candidate_from_row(row: tuple[object, ...]) -> DesktopKnowledgeSourceCandidate:
    return DesktopKnowledgeSourceCandidate(
        evidence_id=str(row[0]),
        document_id=str(row[1]),
        document_name=str(row[2]),
        section=_section(str(row[3])),
        locator=_json_object(str(row[4])),
        excerpt=str(row[5]),
    )


def _source_map_rows(
    connection: sqlite3.Connection, rows: list[tuple[object, ...]]
) -> tuple[DesktopKnowledgeSourceMapEntry, ...]:
    available = _available_sources_in(connection, tuple(str(row[1]) for row in rows))
    entries: list[DesktopKnowledgeSourceMapEntry] = []
    for row in rows:
        evidence_id = str(row[1])
        resolved = available.get(evidence_id)
        entries.append(
            DesktopKnowledgeSourceMapEntry(
                source_id=str(row[0]),
                evidence_id=evidence_id,
                claim_text=str(row[2]),
                document_id=resolved.document_id if resolved else str(row[3]),
                document_name=resolved.document_name if resolved else str(row[4]),
                section=resolved.section if resolved else str(row[5]),
                locator=resolved.locator if resolved else _json_object(str(row[6])),
                excerpt=resolved.excerpt if resolved else str(row[7]),
                availability="available" if resolved else "unavailable",
            )
        )
    return tuple(entries)


def _require_claim_selection(content: str, claim: str) -> None:
    if not claim or content.count(claim) != 1:
        raise ValueError("knowledge_claim_selection_invalid")
    first_line = claim.lstrip().splitlines()[0]
    if first_line.startswith("#") or re.fullmatch(r"[-*+]\s*\[[^]]+\]\([^)]+\)", claim):
        raise ValueError("knowledge_claim_selection_structural")
    selected = _normalized_claim_text(_SOURCE_MARKER.sub("", claim))
    matching_units = [
        unit
        for unit in _claim_units(content)
        if _normalized_claim_text(_SOURCE_MARKER.sub("", unit)) == selected
    ]
    if len(matching_units) != 1:
        raise ValueError("knowledge_claim_selection_invalid")


def _source_search_terms(query: str) -> tuple[str, ...]:
    if len(query) > _SOURCE_SEARCH_QUERY_LIMIT:
        raise ValueError("knowledge_source_query_invalid")
    terms = tuple(dict.fromkeys(value.lower() for value in _TERM_PATTERN.findall(query)))
    if len(terms) > _SOURCE_SEARCH_TERM_LIMIT:
        raise ValueError("knowledge_source_query_invalid")
    return terms


def _claim_units(content: str) -> tuple[str, ...]:
    """Return factual Markdown units while excluding headings and pure navigation."""
    units: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush() -> None:
        if not paragraph:
            return
        value = "\n".join(paragraph).strip()
        paragraph.clear()
        if value and not _is_structural_unit(value):
            units.append(value)

    lines = content.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped:
            flush()
            continue
        if stripped.startswith("#") or (stripped.startswith("[^src-") and "]:" in stripped):
            flush()
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped) or re.fullmatch(r"\|?\s*:?-+:?", stripped):
            flush()
            continue
        if _is_table_separator(stripped):
            flush()
            continue
        if "|" in stripped:
            flush()
            if index + 1 < len(lines) and _is_table_separator(lines[index + 1].strip()):
                continue
            if not _is_structural_unit(stripped):
                units.append(stripped)
            continue
        if re.match(r"^\s*(?:[-*+] |\d+[.)] )", line):
            flush()
            if not _is_structural_unit(stripped):
                units.append(stripped)
            continue
        paragraph.append(stripped)
    flush()
    return tuple(units)


def _is_structural_unit(value: str) -> bool:
    text = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", value.strip())
    text = _SOURCE_MARKER.sub("", text).strip()
    if not text:
        return True
    links = _MARKDOWN_LINK.findall(text)
    without_links = _MARKDOWN_LINK.sub("", text)
    labels_are_structural = all(
        _is_structural_link_label(label, destination) for label, destination in links
    )
    if not re.sub(r"[\s,.;:!?，。；：！？·|/\\—–-]", "", without_links):
        return labels_are_structural
    return labels_are_structural and _is_navigation_unit(without_links, had_link=bool(links))


def _is_structural_link_label(value: str, destination: str) -> bool:
    """Recognize only title-like link labels; ambiguous visible prose stays factual."""
    normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", value.casefold()).strip()
    if not normalized:
        return True
    target = destination.strip().split(maxsplit=1)[0].strip("<>")
    target_name = target.split("#", 1)[0].split("?", 1)[0].rsplit("/", 1)[-1]
    target_stem = target_name.rsplit(".", 1)[0]
    normalized_target = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", target_stem.casefold()).strip()
    if normalized_target and normalized == normalized_target:
        return True

    words = re.findall(r"[A-Za-z0-9]+", value)
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", value))
    if cjk:
        return not words and cjk in {
            "配置",
            "首页",
            "索引",
            "目录",
            "概览",
            "概述",
            "详情",
            "更多",
            "上一节",
            "下一节",
            "上一页",
            "下一页",
        }
    if len(words) == 1:
        return words[0].casefold() in {
            "back",
            "configuration",
            "details",
            "docs",
            "documentation",
            "home",
            "index",
            "menu",
            "more",
            "next",
            "overview",
            "previous",
        }

    structural_targets = {"chapter", "guide", "index", "menu", "page", "section"}
    if words[0].casefold() in {"open", "read", "see", "visit"}:
        return words[-1].casefold() in structural_targets
    return False


def _is_navigation_unit(value: str, *, had_link: bool) -> bool:
    normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", value.casefold()).strip()
    if not normalized:
        return had_link
    words = normalized.split()
    navigation_words = {
        "about",
        "above",
        "back",
        "below",
        "chapter",
        "continue",
        "detail",
        "details",
        "following",
        "for",
        "go",
        "index",
        "learn",
        "menu",
        "more",
        "navigate",
        "next",
        "open",
        "page",
        "please",
        "previous",
        "read",
        "return",
        "section",
        "see",
        "the",
        "this",
        "to",
        "topic",
        "visit",
    }
    if words and all(word in navigation_words for word in words):
        first_action = words[1] if words[0] == "please" and len(words) > 1 else words[0]
        return had_link or first_action in {
            "back",
            "continue",
            "go",
            "next",
            "open",
            "previous",
            "read",
            "return",
            "see",
            "visit",
        }
    compact = normalized.replace(" ", "")
    compact = compact.removeprefix("请")
    chinese_actions = ("参见", "查看", "阅读", "打开", "前往", "继续", "返回", "回到")
    chinese_targets = ("章节", "部分", "页面", "上一节", "下一节", "上一页", "下一页", "详情")
    return compact.startswith(chinese_actions) and (had_link or compact.endswith(chinese_targets))


def _is_table_separator(value: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", value))


def _normalized_claim_text(value: str) -> str:
    value = re.sub(r"[`*_~>]", "", value)
    value = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", value)
    return " ".join(value.split()).casefold()


def _section(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, list):
        return " / ".join(str(item) for item in parsed if str(item))
    return str(parsed)


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
