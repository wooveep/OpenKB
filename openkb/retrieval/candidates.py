"""Scope-first candidate construction for every Desktop retrieval channel."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from openkb.answers.types import DesktopAnswerError, DesktopEvidenceRef
from openkb.knowledge.graph.service import (
    DesktopKnowledgeGraphQueryError,
    bounded_graph_rows,
    graph_query_deadline,
    local_graph_evidence_ids,
)
from openkb.knowledge.pages.source_retrieval import knowledge_source_rows_in
from openkb.retrieval import rows as retrieval_rows
from openkb.retrieval.catalog_retrieval import catalog_route_rows_in
from openkb.retrieval.catalog_store import CatalogGenerationLease
from openkb.retrieval.channels import (
    CATALOG_RETRIEVAL_VARIANTS,
    PAGE_TREE_EVALUATION_VARIANTS,
    DesktopEvaluationVariant,
)
from openkb.retrieval.fusion import (
    GRAPH_CANDIDATE_LIMIT,
    RetrievalCandidate,
    fuse_candidates,
)
from openkb.retrieval.scoped_evidence import ScopedEvidenceView

_CHANNEL_LIMIT = 12
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VariantEvidence:
    evidence: tuple[DesktopEvidenceRef, ...]
    candidates: tuple[RetrievalCandidate, ...]
    protected_candidates: tuple[RetrievalCandidate, ...]
    channel_counts: tuple[tuple[str, int], ...]
    graph_error_code: str | None = None


def catalog_channel_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    catalog: CatalogGenerationLease | None,
    variant: DesktopEvaluationVariant,
    lease_degradations: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView,
) -> tuple[tuple[RetrievalCandidate, ...], tuple[str, ...]]:
    """Drop only the optional Catalog channel when its derived state is invalid."""
    if variant not in CATALOG_RETRIEVAL_VARIANTS:
        return (), ()
    if lease_degradations:
        return (), lease_degradations
    try:
        candidates = _catalog_candidates(
            connection,
            terms,
            catalog.generation_id if catalog is not None else None,
            scoped_view=scoped_view,
        )
        return candidates, _catalog_degradation(connection, catalog, variant)
    except Exception:
        logger.warning("Knowledge Catalog query failed; using baseline retrieval.", exc_info=True)
        return (), ("catalog_query_failed",)


def variant_evidence(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    variant: DesktopEvaluationVariant,
    *,
    scoped_view: ScopedEvidenceView,
    catalog_candidates: tuple[RetrievalCandidate, ...] = (),
    graph_lookup: Callable[..., tuple[str, ...]] = local_graph_evidence_ids,
    graph_row_fetcher: Callable[..., list[tuple[object, ...]]] = bounded_graph_rows,
) -> VariantEvidence:
    """Build one evaluation candidate set after applying the closed document scope."""
    if variant == "fts":
        return _variant_result(_fts_candidates(connection, terms, scoped_view=scoped_view))
    if variant == "structure_lexical":
        return _variant_result(
            _structure_lexical_candidates(connection, terms, scoped_view=scoped_view)
        )
    if variant in PAGE_TREE_EVALUATION_VARIANTS:
        protected = _structure_lexical_candidates(connection, terms, scoped_view=scoped_view)
        return _variant_result(protected + catalog_candidates, protected=protected)
    if variant == "wiki":
        candidates = (
            _wiki_candidates(connection, terms, scoped_view=scoped_view)
            + _knowledge_source_candidates(connection, terms, scoped_view=scoped_view)
            + catalog_candidates
        )
        return _variant_result(candidates)

    protected = _fts_candidates(
        connection, terms, scoped_view=scoped_view
    ) + _structure_lexical_candidates(connection, terms, scoped_view=scoped_view)
    candidates = (
        protected
        + _wiki_candidates(connection, terms, scoped_view=scoped_view)
        + _knowledge_source_candidates(connection, terms, scoped_view=scoped_view)
        + catalog_candidates
    )
    baseline = fuse_candidates(candidates, protected=protected)
    if variant == "baseline":
        return _variant_result(candidates, protected=protected)
    try:
        graph_candidates = _graph_candidates(
            connection,
            terms,
            baseline,
            scoped_view=scoped_view,
            graph_lookup=graph_lookup,
            graph_row_fetcher=graph_row_fetcher,
        )
    except DesktopKnowledgeGraphQueryError as error:
        return _variant_result(
            candidates,
            protected=protected,
            graph_error_code=error.code,
            extra_channel_counts=(("knowledge_graph", 0),),
        )
    bounded_graph = graph_candidates[:GRAPH_CANDIDATE_LIMIT]
    return _variant_result(
        (*candidates, *bounded_graph),
        protected=protected,
        extra_channel_counts=(("knowledge_graph", len(bounded_graph)),),
    )


def page_tree_candidates(
    connection: sqlite3.Connection,
    evidence_ids: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView,
) -> tuple[RetrievalCandidate, ...]:
    """Resolve selected tree bindings only inside the closed document scope."""
    if not evidence_ids:
        return ()
    occurrence_cte, scope_parameters = scoped_view.sql_cte("available_evidence_occurrences")
    rows = connection.execute(
        f"""
        {occurrence_cte}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1
          AND evidence_id IN ({retrieval_rows.placeholders(evidence_ids)})
        """,
        (*scope_parameters, *evidence_ids),
    ).fetchall()
    rows_by_evidence_id = {str(row[0]): row for row in rows}
    ordered_rows = [
        rows_by_evidence_id[evidence_id]
        for evidence_id in evidence_ids
        if evidence_id in rows_by_evidence_id
    ]
    return retrieval_rows.ranked_candidates(ordered_rows, "document_page_tree")


def _fts_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView,
) -> tuple[RetrievalCandidate, ...]:
    if not terms:
        return ()
    query = " OR ".join(f'"{term}"' for term in terms)
    occurrence_cte, scope_parameters = scoped_view.sql_cte("available_evidence_occurrences")
    try:
        rows = connection.execute(
            f"""
            {occurrence_cte}
            SELECT available_evidence_occurrences.evidence_id,
                available_evidence_occurrences.document_id,
                available_evidence_occurrences.display_name,
                available_evidence_occurrences.heading_path,
                available_evidence_occurrences.locator_json,
                available_evidence_occurrences.text
            FROM evidence_fts
            JOIN available_evidence_occurrences
              ON available_evidence_occurrences.evidence_id = evidence_fts.evidence_id
            WHERE evidence_fts MATCH ?
              AND available_evidence_occurrences.occurrence_rank = 1
            ORDER BY bm25(evidence_fts), available_evidence_occurrences.document_id,
                available_evidence_occurrences.ordinal
            LIMIT ?
            """,
            (*scope_parameters, query, _CHANNEL_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = _like_rows(connection, terms, scoped_view=scoped_view)
    return retrieval_rows.ranked_candidates(rows, "fts")


def _like_rows(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView,
) -> list[tuple[object, ...]]:
    clauses = " OR ".join("lower(text) LIKE ?" for _ in terms)
    occurrence_cte, scope_parameters = scoped_view.sql_cte("available_evidence_occurrences")
    return connection.execute(
        f"""
        {occurrence_cte}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1 AND ({clauses})
        ORDER BY document_id, ordinal
        LIMIT ?
        """,
        (*scope_parameters, *(f"%{term}%" for term in terms), _CHANNEL_LIMIT),
    ).fetchall()


def _structure_lexical_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView,
) -> tuple[RetrievalCandidate, ...]:
    if not terms:
        return ()
    return retrieval_rows.ranked_candidates(
        retrieval_rows.scored_rows(
            connection,
            terms,
            weighted_columns=(("heading_path", 2), ("text", 1)),
            scoped_view=scoped_view,
        ),
        "structure_lexical",
    )


def _wiki_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView,
) -> tuple[RetrievalCandidate, ...]:
    if not terms:
        return ()
    return retrieval_rows.ranked_candidates(
        retrieval_rows.scored_rows(
            connection,
            terms,
            weighted_columns=(("display_name", 2), ("heading_path", 1)),
            scoped_view=scoped_view,
        ),
        "wiki",
    )


def _knowledge_source_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView,
) -> tuple[RetrievalCandidate, ...]:
    return retrieval_rows.ranked_candidates(
        knowledge_source_rows_in(
            connection,
            terms,
            limit=_CHANNEL_LIMIT,
            scoped_view=scoped_view,
        ),
        "knowledge_source",
    )


def _catalog_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    generation_id: str | None,
    *,
    scoped_view: ScopedEvidenceView,
) -> tuple[RetrievalCandidate, ...]:
    if generation_id is None:
        return ()
    rows = catalog_route_rows_in(
        connection,
        generation_id,
        terms,
        limit=_CHANNEL_LIMIT,
        scoped_view=scoped_view,
    )
    values: list[RetrievalCandidate] = []
    for rank, row in enumerate(rows, start=1):
        reference = DesktopEvidenceRef(
            evidence_id=str(row[0]),
            document_id=str(row[1]),
            document_name=str(row[2]),
            section=retrieval_rows.section_from_json(str(row[3])),
            locator=retrieval_rows.json_object(str(row[4])),
            excerpt=str(row[5]),
            channels=("catalog",),
        )
        route_weight = row[6]
        if isinstance(route_weight, bool) or not isinstance(route_weight, (int, float)):
            raise DesktopAnswerError(
                "desktop_catalog_state_invalid",
                "The current Knowledge Catalog contains an invalid route weight.",
            )
        values.append(RetrievalCandidate(reference, "catalog", rank, weight=float(route_weight)))
    return tuple(values)


def _graph_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    baseline: tuple[DesktopEvidenceRef, ...],
    *,
    scoped_view: ScopedEvidenceView,
    graph_lookup: Callable[..., tuple[str, ...]],
    graph_row_fetcher: Callable[..., list[tuple[object, ...]]],
) -> tuple[RetrievalCandidate, ...]:
    deadline = graph_query_deadline()
    evidence_ids = graph_lookup(
        connection,
        terms=terms,
        anchor_evidence_ids=tuple(reference.evidence_id for reference in baseline),
        allowed_document_ids=scoped_view.scope.allowed_document_ids,
        deadline=deadline,
    )
    if not evidence_ids:
        return ()
    occurrence_cte, scope_parameters = scoped_view.sql_cte("available_evidence_occurrences")
    rows = graph_row_fetcher(
        connection,
        f"""
        {occurrence_cte}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1
          AND evidence_id IN ({retrieval_rows.placeholders(evidence_ids)})
        """,
        (*scope_parameters, *evidence_ids),
        deadline,
    )
    rows_by_evidence_id = {str(row[0]): row for row in rows}
    ordered_rows = [
        rows_by_evidence_id[evidence_id]
        for evidence_id in evidence_ids
        if evidence_id in rows_by_evidence_id
    ]
    return retrieval_rows.ranked_candidates(ordered_rows, "knowledge_graph")


def _variant_result(
    candidates: tuple[RetrievalCandidate, ...],
    *,
    protected: tuple[RetrievalCandidate, ...] = (),
    graph_error_code: str | None = None,
    extra_channel_counts: tuple[tuple[str, int], ...] = (),
) -> VariantEvidence:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.channel] = counts.get(candidate.channel, 0) + 1
    counts.update(extra_channel_counts)
    return VariantEvidence(
        evidence=fuse_candidates(candidates, protected=protected),
        candidates=candidates,
        protected_candidates=protected,
        channel_counts=tuple(counts.items()),
        graph_error_code=graph_error_code,
    )


def _catalog_degradation(
    connection: sqlite3.Connection,
    catalog: CatalogGenerationLease | None,
    variant: DesktopEvaluationVariant,
) -> tuple[str, ...]:
    if variant not in CATALOG_RETRIEVAL_VARIANTS:
        return ()
    if catalog is not None:
        return ("catalog_stale",) if catalog.is_stale else ()
    row = connection.execute(
        "SELECT status FROM knowledge_catalog_rebuild_tasks WHERE singleton = 1"
    ).fetchone()
    return ("catalog_unavailable",) if row is not None else ()
