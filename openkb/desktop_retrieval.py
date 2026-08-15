"""Deterministic vectorless evidence retrieval for Desktop grounded answers."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_answer_types import (
    DesktopAnswerError,
    DesktopAnswerSourceImage,
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopRetrievalPlan,
)
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
)
from openkb.desktop_workspace import desktop_state_database_path

_MAX_QUERY_LENGTH = 2_000
_MAX_PLAN_TERMS = 8
_MAX_COMBINED_PLAN_TERMS = 12
_CHANNEL_LIMIT = 12
_EVIDENCE_PACK_LIMIT = 6
_RRF_OFFSET = 60
_TERM_PATTERN = re.compile(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]+")
_CELL_RANGE_PATTERN = re.compile(r"^\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$", re.IGNORECASE)
_SCORE_COLUMNS = frozenset(("display_name", "heading_path", "text"))

_AVAILABLE_EVIDENCE_OCCURRENCES_CTE = """
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
class _Candidate:
    reference: DesktopEvidenceRef
    channel: str
    rank: int


class DesktopEvidenceRetriever:
    """Build an Evidence Pack from Available Knowledge with safe fallbacks."""

    def __init__(self, kb_dir: Path, *, model_gateway: DesktopModelGateway | None = None) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._model_gateway = model_gateway

    def retrieve(
        self, question: str, *, is_cancelled: Callable[[], bool] | None = None
    ) -> DesktopEvidencePack:
        """Plan and retrieve without ever allowing optional model work to block a reply."""
        normalized_question = _validate_question(question)
        plan, degradations = self._plan(normalized_question, is_cancelled=is_cancelled)
        connection = _connect(self._database_path)
        try:
            candidates = (
                _fts_candidates(connection, plan.terms)
                + _page_tree_candidates(connection, plan.terms)
                + _wiki_candidates(connection, plan.terms)
            )
            evidence = _fuse_candidates(candidates)
            source_images = _source_images_for_evidence(connection, evidence, self._kb_dir)
        finally:
            connection.close()
        return DesktopEvidencePack(
            retrieval_plan=plan,
            evidence=evidence,
            degradations=tuple(degradations),
            source_images=source_images,
        )

    def _plan(
        self, question: str, *, is_cancelled: Callable[[], bool] | None = None
    ) -> tuple[DesktopRetrievalPlan, list[str]]:
        fallback = _deterministic_plan(question)
        if self._model_gateway is None:
            return fallback, ["retrieval_plan_unavailable"]
        try:
            result = self._model_gateway.analyze(
                DesktopModelRequest("retrieval_plan", "Grounded answer question", question),
                on_event=lambda _event: None,
                is_cancelled=is_cancelled,
            )
            model_plan = _model_plan(question, result.content)
            return _with_baseline_terms(fallback, model_plan), []
        except DesktopModelCancelledError:
            return fallback, ["retrieval_plan_cancelled"]
        except (DesktopModelCallError, ValueError, json.JSONDecodeError):
            return fallback, ["retrieval_plan_fallback"]


def _connect(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise DesktopAnswerError(
            "desktop_knowledge_base_not_found",
            "Open a Desktop Knowledge Base before asking a question.",
        )
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _validate_question(question: str) -> str:
    if not isinstance(question, str) or not question.strip():
        raise DesktopAnswerError("invalid_question", "Enter a question before asking OpenKB.")
    normalized = " ".join(question.split())
    if len(normalized) > _MAX_QUERY_LENGTH:
        raise DesktopAnswerError(
            "invalid_question", "The question is too long for grounded retrieval."
        )
    return normalized


def _deterministic_plan(question: str) -> DesktopRetrievalPlan:
    return DesktopRetrievalPlan(query=question, terms=_terms(question), source="deterministic")


def _model_plan(question: str, content: str) -> DesktopRetrievalPlan:
    payload = json.loads(_json_object_text(content))
    if not isinstance(payload, dict):
        raise ValueError("Retrieval Plan must be an object.")
    values = payload.get("terms")
    if not isinstance(values, list):
        raise ValueError("Retrieval Plan terms are missing.")
    terms = _terms(" ".join(value for value in values if isinstance(value, str)))
    if not terms:
        raise ValueError("Retrieval Plan terms are empty.")
    return DesktopRetrievalPlan(query=question, terms=terms, source="model")


def _with_baseline_terms(
    baseline: DesktopRetrievalPlan, model_plan: DesktopRetrievalPlan
) -> DesktopRetrievalPlan:
    """Keep deterministic question terms even when a valid model plan is incomplete."""
    terms: list[str] = []
    for value in (*baseline.terms, *model_plan.terms):
        if value not in terms:
            terms.append(value)
        if len(terms) == _MAX_COMBINED_PLAN_TERMS:
            break
    return DesktopRetrievalPlan(
        query=baseline.query,
        terms=tuple(terms),
        source=model_plan.source,
    )


def _json_object_text(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0].strip()
    return stripped


def _terms(value: str) -> tuple[str, ...]:
    terms: list[str] = []
    for match in _TERM_PATTERN.finditer(value.casefold()):
        token = match.group(0)
        values = _cjk_bigrams(token) if _is_cjk(token) else (token,)
        for item in values:
            if item and item not in terms:
                terms.append(item)
            if len(terms) == _MAX_PLAN_TERMS:
                return tuple(terms)
    return tuple(terms)


def _is_cjk(value: str) -> bool:
    return bool(value) and all("\u3400" <= character <= "\u9fff" for character in value)


def _cjk_bigrams(value: str) -> tuple[str, ...]:
    if len(value) <= 2:
        return (value,)
    return tuple(value[index : index + 2] for index in range(len(value) - 1))


def _fts_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    if not terms:
        return ()
    query = " OR ".join(f'"{term}"' for term in terms)
    try:
        rows = connection.execute(
            f"""
            {_AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
            SELECT available_evidence_occurrences.evidence_id,
                available_evidence_occurrences.document_id,
                available_evidence_occurrences.display_name,
                available_evidence_occurrences.heading_path,
                available_evidence_occurrences.locator_json,
                available_evidence_occurrences.text
            FROM evidence_fts
            JOIN available_evidence_occurrences
                ON available_evidence_occurrences.evidence_id = evidence_fts.evidence_id
            WHERE evidence_fts MATCH ? AND available_evidence_occurrences.occurrence_rank = 1
            ORDER BY bm25(evidence_fts), available_evidence_occurrences.document_id,
                available_evidence_occurrences.ordinal
            LIMIT ?
            """,
            (query, _CHANNEL_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = _like_rows(connection, terms)
    return _ranked_candidates(rows, "fts")


def _like_rows(connection: sqlite3.Connection, terms: tuple[str, ...]) -> list[tuple[object, ...]]:
    clauses = " OR ".join("lower(text) LIKE ?" for _ in terms)
    return connection.execute(
        f"""
        {_AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1 AND ({clauses})
        ORDER BY document_id, ordinal
        LIMIT ?
        """,
        tuple(f"%{term}%" for term in terms) + (_CHANNEL_LIMIT,),
    ).fetchall()


def _page_tree_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    if not terms:
        return ()
    return _ranked_candidates(
        _scored_rows(
            connection,
            terms,
            weighted_columns=(("heading_path", 2), ("text", 1)),
        ),
        "page_tree",
    )


def _wiki_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    """Find evidence from canonical published document and section names.

    T19 will add editable concept/entity pages to this logical channel. Until then,
    the imported document names and headings are already published knowledge names,
    so this route stays useful rather than becoming an empty future placeholder.
    """
    if not terms:
        return ()
    return _ranked_candidates(
        _scored_rows(
            connection,
            terms,
            weighted_columns=(
                ("display_name", 2),
                ("heading_path", 1),
            ),
        ),
        "wiki",
    )


def _scored_rows(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    weighted_columns: tuple[tuple[str, int], ...],
) -> list[tuple[object, ...]]:
    """Select a bounded channel ranking from every Available Knowledge occurrence.

    Column names are internal constants supplied by the two retrieval routes;
    only user-derived terms are bound as SQLite parameters.
    """
    score_parts: list[str] = []
    parameters: list[object] = []
    for term in terms:
        for column, weight in weighted_columns:
            if column not in _SCORE_COLUMNS:
                raise ValueError(f"Unsupported Desktop retrieval score column: {column}")
            score_parts.append(f"CASE WHEN instr(lower({column}), ?) > 0 THEN {weight} ELSE 0 END")
            parameters.append(term)
    score_expression = " + ".join(score_parts)
    return connection.execute(
        f"""
        {_AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM (
            SELECT evidence_id, document_id, display_name, heading_path, locator_json, text,
                ({score_expression}) AS channel_score
            FROM available_evidence_occurrences
            WHERE occurrence_rank = 1
        )
        WHERE channel_score > 0
        ORDER BY channel_score DESC, document_id, evidence_id
        LIMIT ?
        """,
        (*parameters, _CHANNEL_LIMIT),
    ).fetchall()


def _ranked_candidates(rows: list[tuple[object, ...]], channel: str) -> tuple[_Candidate, ...]:
    values: list[_Candidate] = []
    for rank, row in enumerate(rows, start=1):
        reference = DesktopEvidenceRef(
            evidence_id=str(row[0]),
            document_id=str(row[1]),
            document_name=str(row[2]),
            section=_section_from_json(str(row[3])),
            locator=_json_object(str(row[4])),
            excerpt=str(row[5]),
            channels=(channel,),
        )
        values.append(_Candidate(reference=reference, channel=channel, rank=rank))
    return tuple(values)


def _section_from_json(value: str) -> str:
    try:
        path = json.loads(value)
    except json.JSONDecodeError:
        return "Document"
    if not isinstance(path, list) or not all(isinstance(item, str) for item in path):
        return "Document"
    return " / ".join(path) if path else "Document"


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _fuse_candidates(candidates: tuple[_Candidate, ...]) -> tuple[DesktopEvidenceRef, ...]:
    scores: defaultdict[str, float] = defaultdict(float)
    channels: defaultdict[str, set[str]] = defaultdict(set)
    references: dict[str, DesktopEvidenceRef] = {}
    channel_first: dict[str, str] = {}
    for candidate in candidates:
        evidence_id = candidate.reference.evidence_id
        scores[evidence_id] += 1 / (_RRF_OFFSET + candidate.rank)
        channels[evidence_id].add(candidate.channel)
        references.setdefault(evidence_id, candidate.reference)
        channel_first.setdefault(candidate.channel, evidence_id)

    selected: list[str] = []
    for channel in ("fts", "page_tree", "wiki"):
        first_evidence_id = channel_first.get(channel)
        if first_evidence_id is not None and first_evidence_id not in selected:
            selected.append(first_evidence_id)
    ranked = sorted(scores, key=lambda evidence_id: (-scores[evidence_id], evidence_id))
    for evidence_id in ranked:
        if len(selected) == _EVIDENCE_PACK_LIMIT:
            break
        if evidence_id not in selected:
            selected.append(evidence_id)
    return tuple(
        DesktopEvidenceRef(
            **{
                **references[key].__dict__,
                "channels": tuple(sorted(channels[key])),
            }
        )
        for key in selected
    )


def _source_images_for_evidence(
    connection: sqlite3.Connection,
    evidence: tuple[DesktopEvidenceRef, ...],
    kb_dir: Path,
) -> tuple[DesktopAnswerSourceImage, ...]:
    """Select only original images that share an exact source location with a citation."""
    document_ids = tuple(dict.fromkeys(reference.document_id for reference in evidence))
    if not document_ids:
        return ()
    placeholders = ", ".join("?" for _ in document_ids)
    rows = connection.execute(
        f"""
        SELECT source_images.source_image_id, source_images.document_id,
            source_documents.display_name, source_images.display_name,
            source_images.media_type, source_images.storage_path, source_images.alt_text,
            source_images.locator_json
        FROM source_images
        JOIN source_documents ON source_documents.document_id = source_images.document_id
        WHERE source_documents.availability = 'available'
            AND source_images.document_id IN ({placeholders})
        ORDER BY source_images.document_id, source_images.ordinal
        """,
        document_ids,
    ).fetchall()
    images_by_document: defaultdict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        images_by_document[str(row[1])].append(row)

    selected: list[DesktopAnswerSourceImage] = []
    selected_ids: set[str] = set()
    for reference in evidence:
        for row in images_by_document[reference.document_id]:
            source_image_id = str(row[0])
            if source_image_id in selected_ids:
                continue
            locator = _json_object(str(row[7]))
            if not _source_image_matches_evidence(source_image_id, locator, reference.locator):
                continue
            file_path = kb_dir / str(row[5])
            if not file_path.is_file():
                continue
            selected_ids.add(source_image_id)
            selected.append(
                DesktopAnswerSourceImage(
                    source_image_id=source_image_id,
                    evidence_id=reference.evidence_id,
                    document_id=str(row[1]),
                    document_name=str(row[2]),
                    name=str(row[3]),
                    media_type=str(row[4]),
                    file_path=str(file_path),
                    alt_text=str(row[6]) if row[6] is not None else None,
                    locator={**locator, "source_image_id": source_image_id},
                )
            )
    return tuple(selected)


def _source_image_matches_evidence(
    source_image_id: str,
    image_locator: dict[str, object],
    evidence_locator: dict[str, object],
) -> bool:
    """Keep image display tied to a stable source position, never document affinity alone."""
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
    end_column = end_column or start_column
    end_row = end_row or start_row
    return (
        _column_number(start_column),
        int(start_row),
        _column_number(end_column),
        int(end_row),
    )


def _column_number(value: str) -> int:
    result = 0
    for character in value.upper():
        result = result * 26 + ord(character) - ord("A") + 1
    return result
