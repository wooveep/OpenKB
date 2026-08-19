"""Source-map adapters shared by structured Knowledge Analysis consumers."""

from __future__ import annotations

import json
import sqlite3

from openkb.desktop_knowledge_generations import KnowledgeGenerationSource
from openkb.desktop_knowledge_sources import (
    _available_sources_in,
    knowledge_source_matches_claim,
)


def bind_generation_sources_to_draft_in(
    connection: sqlite3.Connection,
    page_id: str,
    content_markdown: str,
    sources: tuple[KnowledgeGenerationSource, ...],
    *,
    created_at: str,
) -> None:
    """Copy incoming source mappings whose markers remain in a resolved Working Draft."""
    selected = tuple(
        source
        for source in sources
        if knowledge_source_matches_claim(
            content_markdown, source.source_id, source.claim_text
        )
    )
    available = _available_sources_in(connection, tuple(source.evidence_id for source in selected))
    for source in selected:
        resolved = available.get(source.evidence_id)
        if resolved is None:
            raise ValueError("knowledge_source_unavailable")
        connection.execute(
            """
            INSERT INTO knowledge_page_working_sources (
                page_id, source_id, evidence_id, claim_text, document_id, document_name,
                section, locator_json, excerpt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_id, source_id, claim_text) DO UPDATE SET
                evidence_id = excluded.evidence_id,
                document_id = excluded.document_id,
                document_name = excluded.document_name,
                section = excluded.section,
                locator_json = excluded.locator_json,
                excerpt = excluded.excerpt,
                created_at = excluded.created_at
            """,
            (
                page_id,
                source.source_id,
                source.evidence_id,
                source.claim_text,
                resolved.document_id,
                resolved.document_name,
                resolved.section,
                json.dumps(resolved.locator, ensure_ascii=False, sort_keys=True),
                resolved.excerpt,
                created_at,
            ),
        )
