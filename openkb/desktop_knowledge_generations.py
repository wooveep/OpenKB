"""Published derived-knowledge generations and their Markdown projections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_corpus_benchmark import (
    CORPUS_BENCHMARK_SCHEMA_VERSION,
    corpus_benchmark_report_in,
)
from openkb.desktop_corpus_synthesis_generation import (
    CorpusCandidateInput,
    activate_qualified_corpus_generation_in,
    bind_generation_graph_inputs_in,
    corpus_generation_manifest_in,
    create_pending_corpus_manifest_in,
    fail_corpus_manifest_in,
    qualify_corpus_manifest_in,
    refresh_corpus_identity_mappings_in,
    refresh_corpus_manifest_digest_in,
)
from openkb.desktop_entity_dossier_store import build_generation_entity_dossiers_in
from openkb.desktop_knowledge_metadata import decode_knowledge_labels, encode_knowledge_labels
from openkb.desktop_knowledge_relationships import (
    generation_relationship_issues_in,
    rebuild_generation_relationships_in,
)
from openkb.desktop_knowledge_sources import merge_claim_source_markers
from openkb.desktop_okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    materialize_okf_projection,
    stage_okf_projection_in,
)


@dataclass(frozen=True)
class KnowledgeGenerationSource:
    """One claim-to-Evidence mapping retained with an immutable generation item."""

    source_id: str
    evidence_id: str
    claim_text: str


@dataclass(frozen=True)
class KnowledgeGenerationChange:
    """One selected derived Concept, Entity, or Procedure value for a generation."""

    document_id: str
    kind: str
    title: str
    normalized_title: str
    content_markdown: str
    content_sha256: str
    entity_subtype: str | None = None
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    sources: tuple[KnowledgeGenerationSource, ...] = ()
    analysis_provenance_json: str | None = None
    identity_id: str | None = None


def normalized_knowledge_content(value: str) -> str:
    """Return the stable text form used by derived knowledge comparisons."""
    return "\n".join(
        " ".join(line.split()) for line in value.splitlines() if line.strip()
    ).casefold()


def knowledge_content_sha256(value: str) -> str:
    """Fingerprint a derived knowledge value without retaining its text."""
    return hashlib.sha256(normalized_knowledge_content(value).encode("utf-8")).hexdigest()


def current_generation_id_in(connection: sqlite3.Connection) -> int | None:
    """Return the current immutable generation snapshot, if one was published."""
    row = connection.execute(
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
    ).fetchone()
    return int(row[0]) if row is not None else None


def publish_generation_changes_in(
    connection: sqlite3.Connection,
    *,
    current_generation_id: int | None,
    changes: tuple[KnowledgeGenerationChange, ...],
    now: str,
    activate: bool = True,
) -> int:
    """Create one snapshot with all selected changes, inside the caller's transaction."""
    if not changes and current_generation_id is None:
        raise ValueError("A published knowledge generation needs at least one change.")
    cursor = connection.execute(
        """
        INSERT INTO knowledge_generations (parent_generation_id, created_at)
        VALUES (?, ?)
        """,
        (current_generation_id, now),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Knowledge generation insert did not return an identifier.")
    generation_id = int(cursor.lastrowid)
    if current_generation_id is not None:
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype, aliases_json, tags_json,
                analysis_provenance_json, identity_id
            )
            SELECT ?, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype, aliases_json, tags_json,
                analysis_provenance_json, identity_id
            FROM knowledge_generation_items WHERE generation_id = ?
            """,
            (generation_id, current_generation_id),
        )
        connection.execute(
            """
            INSERT INTO knowledge_generation_item_sources (
                generation_id, item_key, source_id, evidence_id, claim_text
            )
            SELECT ?, item_key, source_id, evidence_id, claim_text
            FROM knowledge_generation_item_sources WHERE generation_id = ?
            """,
            (generation_id, current_generation_id),
        )
    for change in changes:
        _upsert_generation_change_in(connection, generation_id, change, now)
    rebuild_generation_relationships_in(connection, generation_id)
    if activate:
        connection.execute(
            """
            INSERT INTO knowledge_generation_state (singleton, current_generation_id)
            VALUES (1, ?)
            ON CONFLICT(singleton) DO UPDATE
            SET current_generation_id = excluded.current_generation_id
            """,
            (generation_id,),
        )
    return generation_id


def publish_additional_generation_sources_in(
    connection: sqlite3.Connection,
    *,
    current_generation_id: int,
    kind: str,
    normalized_title: str,
    sources: tuple[KnowledgeGenerationSource, ...],
    now: str,
) -> int:
    """Add independent claim support without counting D2 occurrences twice."""
    row = connection.execute(
        """
        SELECT item_key, title, content_markdown, content_sha256, source_document_id,
            entity_subtype, aliases_json, tags_json, analysis_provenance_json,
            identity_id
        FROM knowledge_generation_items
        WHERE generation_id = ? AND kind = ? AND normalized_title = ?
        """,
        (current_generation_id, kind, normalized_title),
    ).fetchone()
    if row is None:
        return current_generation_id
    existing_rows = connection.execute(
        """
        SELECT source_id, evidence_id, claim_text
        FROM knowledge_generation_item_sources
        WHERE generation_id = ? AND item_key = ?
        ORDER BY source_id
        """,
        (current_generation_id, str(row[0])),
    ).fetchall()
    merged = {
        (str(value[1]), normalized_knowledge_content(str(value[2]))): KnowledgeGenerationSource(
            str(value[0]), str(value[1]), str(value[2])
        )
        for value in existing_rows
    }
    previous_count = len(merged)
    for source in sources:
        key = source.evidence_id, normalized_knowledge_content(source.claim_text)
        if key not in merged:
            merged[key] = source
    if len(merged) == previous_count:
        return current_generation_id
    content_markdown = merge_claim_source_markers(str(row[2]), sources)
    return publish_generation_changes_in(
        connection,
        current_generation_id=current_generation_id,
        changes=(
            KnowledgeGenerationChange(
                document_id=str(row[4]),
                kind=kind,
                title=str(row[1]),
                normalized_title=normalized_title,
                content_markdown=content_markdown,
                content_sha256=knowledge_content_sha256(content_markdown),
                entity_subtype=str(row[5]) if row[5] is not None else None,
                aliases=decode_knowledge_labels(row[6]),
                tags=decode_knowledge_labels(row[7]),
                sources=tuple(merged.values()),
                analysis_provenance_json=str(row[8]) if row[8] is not None else None,
                identity_id=str(row[9]) if row[9] is not None else None,
            ),
        ),
        now=now,
    )


def materialize_current_generation(kb_dir: Path) -> None:
    """Restore the complete disposable OKF projection."""
    materialize_okf_projection(kb_dir)


def publish_corpus_generation_in(
    connection: sqlite3.Connection,
    *,
    current_generation_id: int | None,
    changes: tuple[KnowledgeGenerationChange, ...],
    document_ids: tuple[str, ...],
    carry_forward_identity_ids: tuple[str, ...],
    synthesis_schema_version: str,
    now: str,
    candidate_inputs: tuple[CorpusCandidateInput, ...] | None = None,
    language: str = "en",
    dossier_planner=None,
    defer_completion: bool = False,
) -> int | None:
    """Atomically replace generated knowledge with one structurally qualified corpus snapshot."""
    if not changes:
        return current_generation_id
    if any(not change.sources or change.identity_id is None for change in changes):
        raise ValueError("Qualified corpus knowledge requires identities and source bindings.")
    cursor = connection.execute(
        """
        INSERT INTO knowledge_generations (
            parent_generation_id, created_at, qualification_state, synthesis_schema_version
        ) VALUES (?, ?, 'candidate', ?)
        """,
        (current_generation_id, now, synthesis_schema_version),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Corpus knowledge generation insert returned no identifier.")
    generation_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO knowledge_generation_documents (generation_id, document_id)
        VALUES (?, ?)
        """,
        ((generation_id, document_id) for document_id in dict.fromkeys(document_ids)),
    )
    create_pending_corpus_manifest_in(
        connection,
        generation_id=generation_id,
        parent_generation_id=current_generation_id,
        document_ids=_generation_document_ids_in(connection, generation_id),
        now=now,
        candidate_inputs=candidate_inputs,
    )
    if current_generation_id is not None and carry_forward_identity_ids:
        _carry_forward_generation_identities_in(
            connection,
            current_generation_id=current_generation_id,
            generation_id=generation_id,
            identity_ids=carry_forward_identity_ids,
            now=now,
        )
    for change in changes:
        _upsert_generation_change_in(connection, generation_id, change, now)
    refresh_corpus_identity_mappings_in(connection, generation_id, now=now)
    if defer_completion:
        refresh_corpus_manifest_digest_in(connection, generation_id, now=now)
        return generation_id
    return complete_prepared_corpus_generation_in(
        connection,
        generation_id,
        language=language,
        now=now,
        planner=dossier_planner,
    )


def publish_incremental_corpus_generation_in(
    connection: sqlite3.Connection,
    *,
    current_generation_id: int | None,
    changes: tuple[KnowledgeGenerationChange, ...],
    document_ids: tuple[str, ...],
    synthesis_schema_version: str,
    now: str,
    candidate_inputs: tuple[CorpusCandidateInput, ...] | None = None,
    language: str = "en",
    dossier_planner=None,
    defer_completion: bool = False,
) -> int | None:
    """Qualify affected identities while preserving every unaffected current item."""
    if not changes and current_generation_id is None:
        return current_generation_id
    if any(not change.sources or change.identity_id is None for change in changes):
        raise ValueError("Qualified corpus knowledge requires identities and source bindings.")
    generation_id = publish_generation_changes_in(
        connection,
        current_generation_id=current_generation_id,
        changes=changes,
        now=now,
        activate=False,
    )
    connection.execute(
        """
        UPDATE knowledge_generations
        SET qualification_state = 'candidate', synthesis_schema_version = ?
        WHERE generation_id = ?
        """,
        (synthesis_schema_version, generation_id),
    )
    if current_generation_id is not None:
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_generation_documents (generation_id, document_id)
            SELECT ?, document_id FROM knowledge_generation_documents
            WHERE generation_id = ?
            """,
            (generation_id, current_generation_id),
        )
    connection.executemany(
        """
        INSERT OR IGNORE INTO knowledge_generation_documents (generation_id, document_id)
        VALUES (?, ?)
        """,
        ((generation_id, document_id) for document_id in dict.fromkeys(document_ids)),
    )
    create_pending_corpus_manifest_in(
        connection,
        generation_id=generation_id,
        parent_generation_id=current_generation_id,
        document_ids=_generation_document_ids_in(connection, generation_id),
        now=now,
        candidate_inputs=candidate_inputs,
    )
    refresh_corpus_identity_mappings_in(connection, generation_id, now=now)
    if defer_completion:
        refresh_corpus_manifest_digest_in(connection, generation_id, now=now)
        return generation_id
    return complete_prepared_corpus_generation_in(
        connection,
        generation_id,
        language=language,
        now=now,
        planner=dossier_planner,
    )


def complete_prepared_corpus_generation_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    language: str,
    now: str,
    planner=None,
) -> int | None:
    """Finish a prepared generation after model work, then atomically activate it."""
    manifest = corpus_generation_manifest_in(connection, generation_id)
    if manifest is None or manifest.lifecycle_state not in {"pending", "identity_ready"}:
        return current_generation_id_in(connection)
    dossier_issues = build_generation_entity_dossiers_in(
        connection,
        generation_id,
        language="zh" if language == "zh" else "en",
        now=now,
        planner=planner,
    )
    bind_generation_graph_inputs_in(connection, generation_id, now=now)
    rebuild_generation_relationships_in(connection, generation_id)
    refresh_corpus_manifest_digest_in(connection, generation_id, now=now)
    _record_corpus_benchmark_in(connection, generation_id)
    qualification_issues = (
        *dossier_issues,
        *corpus_generation_qualification_issues_in(connection, generation_id),
        *qualify_corpus_manifest_in(connection, generation_id, now=now),
    )
    if qualification_issues:
        lifecycle_state = (
            "superseded"
            if any(
                issue in {"candidate_generation_superseded", "candidate_dependency_unavailable"}
                for issue in qualification_issues
            )
            else "failed"
        )
        connection.execute(
            "UPDATE knowledge_generations SET qualification_state = 'failed' "
            "WHERE generation_id = ?",
            (generation_id,),
        )
        fail_corpus_manifest_in(
            connection,
            generation_id,
            now=now,
            lifecycle_state=lifecycle_state,
        )
        return current_generation_id_in(connection)
    connection.execute(
        "UPDATE knowledge_generations SET qualification_state = 'qualified' "
        "WHERE generation_id = ?",
        (generation_id,),
    )
    return (
        generation_id
        if activate_qualified_corpus_generation_in(connection, generation_id, now=now)
        else current_generation_id_in(connection)
    )


def corpus_generation_qualification_issues_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[str, ...]:
    """Validate the source and identity invariants required before activation."""
    issues: list[str] = []
    item_rows = connection.execute(
        """
        SELECT item_key, content_markdown, content_sha256, identity_id, provenance_state
        FROM knowledge_generation_items WHERE generation_id = ?
        """,
        (generation_id,),
    ).fetchall()
    if not item_rows:
        issues.append("empty_generation")
    if any(
        not str(row[1]).strip()
        or str(row[2]) != knowledge_content_sha256(str(row[1]))
        or row[3] is None
        or str(row[4]) != "source_backed"
        for row in item_rows
    ):
        issues.append("invalid_item")
    missing_sources = connection.execute(
        """
        SELECT COUNT(*)
        FROM knowledge_generation_items AS items
        WHERE items.generation_id = ? AND NOT EXISTS (
            SELECT 1 FROM knowledge_generation_item_sources AS sources
            WHERE sources.generation_id = items.generation_id
              AND sources.item_key = items.item_key
        )
        """,
        (generation_id,),
    ).fetchone()
    if missing_sources is not None and int(missing_sources[0]) > 0:
        issues.append("missing_item_source")
    invalid_sources = connection.execute(
        """
        SELECT COUNT(*)
        FROM knowledge_generation_item_sources AS sources
        WHERE sources.generation_id = ? AND (
            trim(sources.claim_text) = '' OR NOT EXISTS (
                SELECT 1 FROM evidence_occurrences AS occurrences
                JOIN source_documents AS documents
                  ON documents.document_id = occurrences.document_id
                WHERE occurrences.evidence_id = sources.evidence_id
                  AND documents.availability = 'available'
            )
        )
        """,
        (generation_id,),
    ).fetchone()
    if invalid_sources is not None and int(invalid_sources[0]) > 0:
        issues.append("invalid_item_source")
    issues.extend(generation_relationship_issues_in(connection, generation_id))
    duplicates = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT identity_id
            FROM knowledge_generation_items
            WHERE generation_id = ?
            GROUP BY identity_id HAVING COUNT(*) > 1
        )
        """,
        (generation_id,),
    ).fetchone()
    if duplicates is not None and int(duplicates[0]) > 0:
        issues.append("duplicate_identity")
    benchmark_row = connection.execute(
        "SELECT qualification_report_json FROM knowledge_generations WHERE generation_id = ?",
        (generation_id,),
    ).fetchone()
    try:
        benchmark = (
            json.loads(str(benchmark_row[0])) if benchmark_row and benchmark_row[0] else None
        )
    except json.JSONDecodeError:
        benchmark = None
    if (
        not isinstance(benchmark, dict)
        or benchmark.get("schema_version") != CORPUS_BENCHMARK_SCHEMA_VERSION
        or benchmark.get("passed") is not True
    ):
        issues.append("corpus_benchmark_failed")
    return tuple(issues)


def _record_corpus_benchmark_in(connection: sqlite3.Connection, generation_id: int) -> None:
    report = corpus_benchmark_report_in(connection, generation_id)
    connection.execute(
        "UPDATE knowledge_generations SET qualification_report_json = ? WHERE generation_id = ?",
        (report.as_json(), generation_id),
    )


def _generation_document_ids_in(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT document_id FROM knowledge_generation_documents "
            "WHERE generation_id = ? ORDER BY document_id",
            (generation_id,),
        ).fetchall()
    )


def _carry_forward_generation_identities_in(
    connection: sqlite3.Connection,
    *,
    current_generation_id: int,
    generation_id: int,
    identity_ids: tuple[str, ...],
    now: str,
) -> None:
    requested_placeholders = ", ".join("?" for _ in identity_ids)
    binding_rows = connection.execute(
        f"""
        SELECT mappings.identity_id, mappings.candidate_id, mappings.match_basis
        FROM knowledge_generation_identity_mappings AS mappings
        JOIN knowledge_generation_candidate_inputs AS inputs
          ON inputs.generation_id = ?
         AND inputs.candidate_generation_id = mappings.candidate_generation_id
        JOIN knowledge_candidate_generation_candidates AS candidates
          ON candidates.candidate_generation_id = mappings.candidate_generation_id
         AND candidates.candidate_id = mappings.candidate_id
         AND candidates.admission_state = 'admitted'
        WHERE mappings.generation_id = ?
          AND mappings.identity_id IN ({requested_placeholders})
        ORDER BY mappings.identity_id, mappings.candidate_id
        """,
        (generation_id, current_generation_id, *identity_ids),
    ).fetchall()
    mappable_identity_ids = tuple(dict.fromkeys(str(row[0]) for row in binding_rows))
    if not mappable_identity_ids:
        return
    connection.executemany(
        """
        INSERT INTO knowledge_identity_candidates (
            identity_id, candidate_id, match_basis, created_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(identity_id, candidate_id) DO UPDATE SET
            match_basis = excluded.match_basis,
            created_at = excluded.created_at
        """,
        ((str(row[0]), str(row[1]), str(row[2]), now) for row in binding_rows),
    )
    placeholders = ", ".join("?" for _ in mappable_identity_ids)
    parameters = (generation_id, current_generation_id, *mappable_identity_ids)
    connection.execute(
        f"""
        INSERT INTO knowledge_generation_items (
            generation_id, item_key, kind, title, normalized_title,
            content_markdown, content_sha256, source_document_id, created_at,
            provenance_state, entity_subtype, aliases_json, tags_json,
            analysis_provenance_json, identity_id
        )
        SELECT ?, item_key, kind, title, normalized_title,
            content_markdown, content_sha256, source_document_id, created_at,
            provenance_state, entity_subtype, aliases_json, tags_json,
            analysis_provenance_json, identity_id
        FROM knowledge_generation_items
        WHERE generation_id = ? AND identity_id IN ({placeholders})
        """,
        parameters,
    )
    connection.execute(
        f"""
        INSERT INTO knowledge_generation_item_sources (
            generation_id, item_key, source_id, evidence_id, claim_text
        )
        SELECT ?, sources.item_key, sources.source_id,
            sources.evidence_id, sources.claim_text
        FROM knowledge_generation_item_sources AS sources
        JOIN knowledge_generation_items AS items
          ON items.generation_id = sources.generation_id
         AND items.item_key = sources.item_key
        WHERE sources.generation_id = ? AND items.identity_id IN ({placeholders})
        """,
        parameters,
    )


def materialize_generation_in(
    connection: sqlite3.Connection, kb_dir: Path, generation_id: int | None
) -> None:
    """Rebuild the visible projection for the current committed generation."""
    staged = stage_generation_projection_in(connection, kb_dir, generation_id)
    try:
        activate_generation_projection(kb_dir, staged)
    finally:
        discard_generation_projection_staging(staged)


def stage_generation_projection_in(
    connection: sqlite3.Connection, kb_dir: Path, _generation_id: int | None
) -> Path:
    """Stage the complete bundle containing the transactional generation."""
    return stage_okf_projection_in(connection, kb_dir)


def activate_generation_projection(kb_dir: Path, staged: Path) -> None:
    """Activate the complete bundle after the generation transaction commits."""
    activate_okf_projection(kb_dir, staged)


def discard_generation_projection_staging(staged: Path) -> None:
    """Delete a hidden complete-bundle projection."""
    discard_okf_projection_staging(staged)


def _upsert_generation_change_in(
    connection: sqlite3.Connection,
    generation_id: int,
    change: KnowledgeGenerationChange,
    now: str,
) -> None:
    existing = connection.execute(
        """
        SELECT item_key FROM knowledge_generation_items
        WHERE generation_id = ? AND kind = ? AND normalized_title = ?
        """,
        (generation_id, change.kind, change.normalized_title),
    ).fetchone()
    if existing is None:
        item_key = change.identity_id or uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype, aliases_json, tags_json,
                analysis_provenance_json, identity_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                item_key,
                change.kind,
                change.title,
                change.normalized_title,
                change.content_markdown,
                change.content_sha256,
                change.document_id,
                now,
                "source_backed" if change.sources else "legacy_unmapped",
                change.entity_subtype,
                encode_knowledge_labels(change.aliases),
                encode_knowledge_labels(change.tags),
                change.analysis_provenance_json,
                change.identity_id,
            ),
        )
    else:
        item_key = str(existing[0])
        connection.execute(
            """
            UPDATE knowledge_generation_items
            SET title = ?, content_markdown = ?, content_sha256 = ?,
                source_document_id = ?, created_at = ?, provenance_state = ?,
                entity_subtype = ?, aliases_json = ?, tags_json = ?,
                analysis_provenance_json = ?, identity_id = ?
            WHERE generation_id = ? AND item_key = ?
            """,
            (
                change.title,
                change.content_markdown,
                change.content_sha256,
                change.document_id,
                now,
                "source_backed" if change.sources else "legacy_unmapped",
                change.entity_subtype,
                encode_knowledge_labels(change.aliases),
                encode_knowledge_labels(change.tags),
                change.analysis_provenance_json,
                change.identity_id,
                generation_id,
                item_key,
            ),
        )
        connection.execute(
            """
            DELETE FROM knowledge_generation_item_sources
            WHERE generation_id = ? AND item_key = ?
            """,
            (generation_id, item_key),
        )
    connection.executemany(
        """
        INSERT INTO knowledge_generation_item_sources (
            generation_id, item_key, source_id, evidence_id, claim_text
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (generation_id, item_key, source.source_id, source.evidence_id, source.claim_text)
            for source in change.sources
        ),
    )
