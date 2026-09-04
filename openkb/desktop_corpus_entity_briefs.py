"""Bounded, generation-aware Corpus Entity Brief recall for document Inventory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from openkb.desktop_document_entity_inventory import CorpusEntityBrief
from openkb.desktop_knowledge_analysis import DesktopKnowledgeAnalysis
from openkb.desktop_knowledge_rendering import UNSPECIFIED_APPLICABILITY
from openkb.desktop_knowledge_titles import (
    controlled_latin_title_key,
    normalize_knowledge_title,
)

_MAX_CORPUS_ENTITY_BRIEFS = 32


def load_relevant_corpus_entity_briefs(
    database_path: Path,
    analysis: DesktopKnowledgeAnalysis,
) -> tuple[CorpusEntityBrief, ...]:
    """Recall only current-generation identities with deterministic name overlap."""
    proposal_names = tuple(
        dict.fromkeys(
            name
            for candidate in analysis.entities
            for name in (candidate.title, *candidate.aliases)
            if name.strip()
        )
    )
    if not proposal_names:
        return ()
    normalized_names = frozenset(normalize_knowledge_title(name)[1] for name in proposal_names)
    controlled_names = frozenset(controlled_latin_title_key(name) for name in proposal_names)
    placeholders = ", ".join("?" for _value in normalized_names)
    controlled_placeholders = ", ".join("?" for _value in controlled_names)
    connection = sqlite3.connect(database_path)
    try:
        current = connection.execute(
            "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
        ).fetchone()
        if current is None:
            return ()
        generation_id = int(current[0])
        rows = connection.execute(
            f"""
            SELECT DISTINCT items.identity_id, identities.canonical_title,
                items.entity_subtype, identities.status, identities.normalized_title
            FROM knowledge_generation_items AS items
            JOIN knowledge_identities AS identities
              ON identities.identity_id = items.identity_id
            LEFT JOIN knowledge_identity_aliases AS aliases
              ON aliases.identity_id = identities.identity_id
            WHERE items.generation_id = ? AND items.kind = 'entity'
              AND (
                identities.normalized_title IN ({placeholders})
                OR aliases.normalized_alias IN ({placeholders})
                OR replace(replace(replace(identities.normalized_title, ' ', ''), '-', ''), '_', '')
                    IN ({controlled_placeholders})
                OR replace(
                    replace(
                        replace(COALESCE(aliases.normalized_alias, ''), ' ', ''),
                        '-', ''
                    ),
                    '_', ''
                )
                    IN ({controlled_placeholders})
              )
            ORDER BY identities.normalized_title, items.identity_id
            """,
            (
                generation_id,
                *normalized_names,
                *normalized_names,
                *controlled_names,
                *controlled_names,
            ),
        ).fetchall()
        briefs = tuple(
            brief
            for row in rows
            if (
                brief := _brief_in(
                    connection,
                    generation_id=generation_id,
                    row=row,
                    normalized_names=normalized_names,
                    controlled_names=controlled_names,
                )
            )
            is not None
        )
        return tuple(
            sorted(
                briefs,
                key=lambda brief: (
                    "exact_title" not in brief.match_signals,
                    "exact_alias" not in brief.match_signals,
                    brief.canonical_title.casefold(),
                    brief.identity_id,
                ),
            )[:_MAX_CORPUS_ENTITY_BRIEFS]
        )
    finally:
        connection.close()


def _brief_in(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    row: tuple[object, ...],
    normalized_names: frozenset[str],
    controlled_names: frozenset[str],
) -> CorpusEntityBrief | None:
    identity_id = str(row[0])
    canonical_title = str(row[1])
    normalized_title = str(row[4])
    aliases = tuple(
        str(value[0])
        for value in connection.execute(
            "SELECT alias FROM knowledge_identity_aliases WHERE identity_id = ? "
            "ORDER BY normalized_alias",
            (identity_id,),
        )
    )
    alias_normalized = frozenset(normalize_knowledge_title(alias)[1] for alias in aliases)
    match_signals: list[str] = []
    if normalized_title in normalized_names:
        match_signals.append("exact_title")
    if alias_normalized & normalized_names:
        match_signals.append("exact_alias")
    if controlled_latin_title_key(canonical_title) in controlled_names or any(
        controlled_latin_title_key(alias) in controlled_names for alias in aliases
    ):
        match_signals.append("controlled_separator")
    if not match_signals:
        return None
    claim_rows = connection.execute(
        """
        SELECT mappings.candidate_generation_id, mappings.candidate_id,
            claims.claim_ordinal, claims.role, claims.claim_text,
            claims.applicability_json, generations.document_id
        FROM knowledge_generation_identity_mappings AS mappings
        JOIN knowledge_candidate_generation_claims AS claims
          ON claims.candidate_generation_id = mappings.candidate_generation_id
         AND claims.candidate_id = mappings.candidate_id
        JOIN knowledge_candidate_generations AS generations
          ON generations.candidate_generation_id = mappings.candidate_generation_id
        WHERE mappings.generation_id = ? AND mappings.identity_id = ?
        ORDER BY CASE claims.role WHEN 'definition' THEN 0 WHEN 'purpose' THEN 1 ELSE 2 END,
            mappings.candidate_generation_id, mappings.candidate_id, claims.claim_ordinal
        """,
        (generation_id, identity_id),
    ).fetchall()
    if not claim_rows:
        return None
    applicability_values: defaultdict[str, set[str]] = defaultdict(set)
    for claim_row in claim_rows:
        try:
            applicability = json.loads(str(claim_row[5]))
        except json.JSONDecodeError:
            continue
        if not isinstance(applicability, dict):
            continue
        for dimension, value in applicability.items():
            if isinstance(value, str) and value and value != UNSPECIFIED_APPLICABILITY:
                applicability_values[str(dimension)].add(value)
    stable_applicability = tuple(
        (dimension, next(iter(values)))
        for dimension, values in sorted(applicability_values.items())
        if len(values) == 1
    )
    claim_keys = {(str(value[0]), str(value[1]), int(value[2])) for value in claim_rows}
    documents = {str(value[6]) for value in claim_rows}
    return CorpusEntityBrief(
        brief_id="brief-"
        + hashlib.sha256(f"{generation_id}\x1f{identity_id}".encode("utf-8")).hexdigest(),
        identity_id=identity_id,
        canonical_title=canonical_title,
        aliases=aliases,
        entity_subtype=str(row[2]) if row[2] is not None else None,
        description=str(claim_rows[0][4]),
        source_document_count=len(documents),
        current_claim_count=len(claim_keys),
        generation_id=generation_id,
        applicability=stable_applicability,
        review_state=str(row[3]),
        match_signals=tuple(dict.fromkeys(match_signals)),
    )
