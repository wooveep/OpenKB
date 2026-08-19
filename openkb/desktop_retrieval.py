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
from openkb.desktop_graph_feature_flags import local_graph_default_enabled
from openkb.desktop_knowledge_graph import (
    DesktopKnowledgeGraphQueryError,
    bounded_graph_rows,
    graph_query_deadline,
    local_graph_evidence_ids,
    record_query_diagnostic,
)
from openkb.desktop_knowledge_sources import (
    AVAILABLE_EVIDENCE_OCCURRENCES_CTE,
    knowledge_source_rows_in,
)
from openkb.desktop_lexical import cjk_bigrams, is_cjk_text
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
)
from openkb.desktop_retrieval_channels import (
    DESKTOP_RETRIEVAL_VARIANTS,
    DesktopRetrievalVariant,
)
from openkb.desktop_workspace import desktop_state_database_path

_MAX_QUERY_LENGTH = 2_000
_MAX_PLAN_TERMS = 8
_MAX_COMBINED_PLAN_TERMS = 12
_CHANNEL_LIMIT = 12
_EVIDENCE_PACK_LIMIT = 6
DESKTOP_EVIDENCE_RECALL_K = _EVIDENCE_PACK_LIMIT
_BASELINE_MINIMUM_QUOTA = 4
_GRAPH_CANDIDATE_LIMIT = 2
_RRF_OFFSET = 60
_TERM_PATTERN = re.compile(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]+")
_CELL_RANGE_PATTERN = re.compile(r"^\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$", re.IGNORECASE)
_SCORE_COLUMNS = frozenset(("display_name", "heading_path", "text"))


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
        variant: DesktopRetrievalVariant = (
            "local_graph" if local_graph_default_enabled(self._kb_dir) else "baseline"
        )
        return self.retrieve_variant(question, variant=variant, is_cancelled=is_cancelled)

    def build_plan(
        self, question: str, *, is_cancelled: Callable[[], bool] | None = None
    ) -> tuple[DesktopRetrievalPlan, tuple[str, ...]]:
        """Build one bounded plan that an evaluation can reuse across variants."""
        normalized_question = _validate_question(question)
        plan, degradations = self._plan(normalized_question, is_cancelled=is_cancelled)
        return plan, tuple(degradations)

    def retrieve_variant(
        self,
        question: str,
        *,
        variant: DesktopRetrievalVariant,
        retrieval_plan: DesktopRetrievalPlan | None = None,
        degradations: tuple[str, ...] = (),
        is_cancelled: Callable[[], bool] | None = None,
    ) -> DesktopEvidencePack:
        """Retrieve one named vectorless channel for a fixed evaluation plan.

        Normal answers select ``baseline`` until an approved evaluation enables
        ``local_graph``.  The narrower variants are exposed so the regression
        harness can compare like-for-like candidate sets without giving each
        variant a different query plan.
        """
        if variant not in DESKTOP_RETRIEVAL_VARIANTS:
            raise ValueError(f"Unsupported Desktop retrieval variant: {variant}")
        normalized_question = _validate_question(question)
        if retrieval_plan is None:
            plan, plan_degradations = self._plan(normalized_question, is_cancelled=is_cancelled)
            all_degradations = tuple((*degradations, *plan_degradations))
        else:
            if retrieval_plan.query != normalized_question:
                raise DesktopAnswerError(
                    "desktop_retrieval_plan_invalid",
                    "The evaluation retrieval plan does not match the question.",
                )
            plan = retrieval_plan
            all_degradations = degradations
        graph_error_code: str | None = None
        connection = _connect(self._database_path)
        try:
            evidence, graph_error_code = _variant_evidence(connection, plan.terms, variant)
            source_images = _source_images_for_evidence(connection, evidence, self._kb_dir)
        finally:
            connection.close()
        if graph_error_code is not None:
            record_query_diagnostic(self._kb_dir, graph_error_code)
        return DesktopEvidencePack(
            retrieval_plan=plan,
            evidence=evidence,
            degradations=all_degradations,
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
        values = cjk_bigrams(token) if is_cjk_text(token) else (token,)
        for item in values:
            if item and item not in terms:
                terms.append(item)
            if len(terms) == _MAX_PLAN_TERMS:
                return tuple(terms)
    return tuple(terms)


def _fts_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    if not terms:
        return ()
    query = " OR ".join(f'"{term}"' for term in terms)
    try:
        rows = connection.execute(
            f"""
            {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
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
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1 AND ({clauses})
        ORDER BY document_id, ordinal
        LIMIT ?
        """,
        tuple(f"%{term}%" for term in terms) + (_CHANNEL_LIMIT,),
    ).fetchall()


def _structure_lexical_candidates(
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
        "structure_lexical",
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


def _knowledge_generation_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    """Route published derived knowledge back to its available source evidence."""
    if not terms:
        return ()
    score_parts: list[str] = []
    parameters: list[object] = []
    for term in terms:
        score_parts.extend(
            (
                "CASE WHEN instr(lower(items.title), ?) > 0 THEN 2 ELSE 0 END",
                "CASE WHEN instr(lower(items.content_markdown), ?) > 0 THEN 1 ELSE 0 END",
            )
        )
        parameters.extend((term, term))
    score_expression = " + ".join(score_parts)
    rows = connection.execute(
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM (
            SELECT available_evidence_occurrences.evidence_id,
                available_evidence_occurrences.document_id,
                available_evidence_occurrences.display_name,
                available_evidence_occurrences.heading_path,
                available_evidence_occurrences.locator_json,
                available_evidence_occurrences.text,
                ({score_expression}) AS channel_score,
                items.item_key, available_evidence_occurrences.ordinal
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            JOIN available_evidence_occurrences
                ON available_evidence_occurrences.document_id = items.source_document_id
            WHERE state.singleton = 1 AND available_evidence_occurrences.occurrence_rank = 1
        )
        WHERE channel_score > 0
        ORDER BY channel_score DESC, item_key, ordinal
        LIMIT ?
        """,
        (*parameters, _CHANNEL_LIMIT),
    ).fetchall()
    return _ranked_candidates(rows, "knowledge_generation")


def _knowledge_source_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    """Use published claim wording only to route back to its mapped original evidence."""
    return _ranked_candidates(
        knowledge_source_rows_in(connection, terms, limit=_CHANNEL_LIMIT),
        "knowledge_source",
    )


def _graph_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    baseline: tuple[DesktopEvidenceRef, ...],
) -> tuple[_Candidate, ...]:
    """Resolve a bounded graph neighborhood back to available EvidenceRefs."""
    deadline = graph_query_deadline()
    evidence_ids = local_graph_evidence_ids(
        connection,
        terms=terms,
        anchor_evidence_ids=tuple(reference.evidence_id for reference in baseline),
        deadline=deadline,
    )
    if not evidence_ids:
        return ()
    rows = bounded_graph_rows(
        connection,
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1 AND evidence_id IN ({_placeholders(evidence_ids)})
        """,
        evidence_ids,
        deadline,
    )
    rows_by_evidence_id = {str(row[0]): row for row in rows}
    ordered_rows = [
        rows_by_evidence_id[evidence_id]
        for evidence_id in evidence_ids
        if evidence_id in rows_by_evidence_id
    ]
    return _ranked_candidates(ordered_rows, "knowledge_graph")


def _variant_evidence(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    variant: DesktopRetrievalVariant,
) -> tuple[tuple[DesktopEvidenceRef, ...], str | None]:
    """Build one evaluation candidate set without adding unrequested channels."""
    if variant == "fts":
        return _fuse_candidates(_fts_candidates(connection, terms)), None
    if variant == "structure_lexical":
        return _fuse_candidates(_structure_lexical_candidates(connection, terms)), None
    if variant == "wiki":
        return _fuse_candidates(
            _wiki_candidates(connection, terms)
            + _knowledge_generation_candidates(connection, terms)
            + _knowledge_source_candidates(connection, terms)
        ), None

    baseline = _fuse_candidates(
        _fts_candidates(connection, terms)
        + _structure_lexical_candidates(connection, terms)
        + _wiki_candidates(connection, terms)
        + _knowledge_generation_candidates(connection, terms)
        + _knowledge_source_candidates(connection, terms)
    )
    if variant == "baseline":
        return baseline, None
    try:
        graph_candidates = _graph_candidates(connection, terms, baseline)
    except DesktopKnowledgeGraphQueryError as error:
        # A failed graph capability is never user-visible and never removes
        # the independently retrieved baseline.
        return _with_graph_budget(baseline, ()), error.code
    return _with_graph_budget(baseline, graph_candidates), None


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
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
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
    for channel in ("fts", "structure_lexical", "wiki"):
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


def _with_graph_budget(
    baseline: tuple[DesktopEvidenceRef, ...], graph: tuple[_Candidate, ...]
) -> tuple[DesktopEvidenceRef, ...]:
    """Reserve baseline evidence before an optional graph can add context."""
    graph_references = {candidate.reference.evidence_id: candidate.reference for candidate in graph}

    def with_graph_channel(reference: DesktopEvidenceRef) -> DesktopEvidenceRef:
        graph_reference = graph_references.get(reference.evidence_id)
        if graph_reference is None:
            return reference
        return DesktopEvidenceRef(
            **{
                **reference.__dict__,
                "channels": tuple(sorted(set(reference.channels) | set(graph_reference.channels))),
            }
        )

    baseline_references = tuple(with_graph_channel(reference) for reference in baseline)
    baseline_by_evidence_id = {
        reference.evidence_id: reference for reference in baseline_references
    }
    selected: list[DesktopEvidenceRef] = []
    selected_ids: set[str] = set()

    def append(reference: DesktopEvidenceRef) -> None:
        if reference.evidence_id not in selected_ids and len(selected) < _EVIDENCE_PACK_LIMIT:
            selected.append(reference)
            selected_ids.add(reference.evidence_id)

    for reference in baseline_references[:_BASELINE_MINIMUM_QUOTA]:
        append(reference)
    graph_added = 0
    for candidate in graph:
        if graph_added == _GRAPH_CANDIDATE_LIMIT:
            break
        reference = baseline_by_evidence_id.get(
            candidate.reference.evidence_id, candidate.reference
        )
        if reference.evidence_id in selected_ids:
            continue
        append(reference)
        graph_added += 1
    for reference in baseline_references:
        append(reference)
    return tuple(selected)


def _placeholders(values: tuple[str, ...]) -> str:
    if not values:
        raise ValueError("Evidence lookup requires at least one identifier.")
    return ", ".join("?" for _ in values)


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
