"""Deterministic D0–D2 content reuse for Desktop document imports.

The first stored copy of content owns its normalized processing result.  Later
raw assets remain distinct source documents, while exact body and evidence
matches point at that result rather than creating another independent support.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from openkb.desktop_import_artifacts import DocumentIRBlock, SourceImage
from openkb.desktop_import_types import DesktopDeduplication, DesktopImportedDocument
from openkb.desktop_source_image_assets import persist_source_images


class _ImportState(Protocol):
    @property
    def job_id(self) -> str: ...

    @property
    def recovery_run_id(self) -> str | None: ...

    @property
    def stage_ids(self) -> dict[str, str]: ...


CompleteJob = Callable[[sqlite3.Connection, str, str, str, str], None]


def normalized_body_sha256(blocks: tuple[DocumentIRBlock, ...]) -> str:
    """Hash the normalized, structured body while excluding source-specific locations."""
    payload = [
        {
            "kind": block.kind,
            "heading_path": [_normalized_text(value) for value in block.heading_path],
            "text": _normalized_text(block.text),
        }
        for block in blocks
    ]
    return _sha256({"schema": "document-body-v1", "blocks": payload})


def evidence_sha256(block: DocumentIRBlock) -> str:
    """Hash one evidence fragment with enough structure to preserve its context."""
    return _sha256(
        {
            "schema": "evidence-v1",
            "kind": block.kind,
            "heading_path": [_normalized_text(value) for value in block.heading_path],
            "text": _normalized_text(block.text),
        }
    )


def backfill_deduplication_metadata(connection: sqlite3.Connection, *, created_at: str) -> None:
    """Attach D1/D2 lookup records to databases created before migration 12."""
    blocks_by_document: dict[str, list[DocumentIRBlock]] = defaultdict(list)
    rows = connection.execute(
        """
        SELECT document_id, block_id, ordinal, kind, text, heading_path
        FROM document_ir_blocks
        ORDER BY document_id, ordinal
        """
    ).fetchall()
    for document_id, block_id, ordinal, kind, text, heading_path in rows:
        blocks_by_document[str(document_id)].append(
            DocumentIRBlock(
                block_id=str(block_id),
                ordinal=int(ordinal),
                kind=str(kind),
                text=str(text),
                heading_path=_heading_path(str(heading_path)),
                line_start=1,
                line_end=1,
            )
        )
    for document_id, blocks in blocks_by_document.items():
        existing = connection.execute(
            "SELECT 1 FROM document_content_fingerprints WHERE document_id = ?", (document_id,)
        ).fetchone()
        if existing is not None:
            continue
        body_hash = normalized_body_sha256(tuple(blocks))
        canonical = connection.execute(
            """
            SELECT document_id
            FROM document_content_fingerprints
            WHERE normalized_body_sha256 = ? AND canonical_document_id IS NULL
            ORDER BY document_id
            LIMIT 1
            """,
            (body_hash,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO document_content_fingerprints (
                document_id, normalized_body_sha256, canonical_document_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                document_id,
                body_hash,
                str(canonical[0]) if canonical is not None else None,
                created_at,
            ),
        )

    evidence_rows = connection.execute(
        """
        SELECT evidence_refs.evidence_id, evidence_refs.document_id, evidence_refs.block_id,
            evidence_refs.ordinal, document_ir_blocks.kind, document_ir_blocks.text,
            document_ir_blocks.heading_path
        FROM evidence_refs
        JOIN document_ir_blocks ON document_ir_blocks.block_id = evidence_refs.block_id
        ORDER BY evidence_refs.evidence_id
        """
    ).fetchall()
    for evidence_id, document_id, block_id, ordinal, kind, text, heading_path in evidence_rows:
        block = DocumentIRBlock(
            block_id=str(block_id),
            ordinal=int(ordinal),
            kind=str(kind),
            text=str(text),
            heading_path=_heading_path(str(heading_path)),
            line_start=1,
            line_end=1,
        )
        fingerprint = evidence_sha256(block)
        connection.execute(
            """
            INSERT INTO evidence_fingerprints (evidence_sha256, evidence_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(evidence_sha256) DO NOTHING
            """,
            (fingerprint, str(evidence_id), created_at),
        )
        canonical = connection.execute(
            "SELECT evidence_id FROM evidence_fingerprints WHERE evidence_sha256 = ?",
            (fingerprint,),
        ).fetchone()
        if canonical is None:
            raise RuntimeError("Evidence fingerprint backfill did not retain a canonical record.")
        connection.execute(
            """
            INSERT INTO evidence_occurrences (document_id, block_id, evidence_id, ordinal)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(document_id, block_id) DO NOTHING
            """,
            (document_id, block_id, str(canonical[0]), int(ordinal)),
        )


def deduplication_backfill_needed(connection: sqlite3.Connection) -> bool:
    """Return whether a pre-D12 record still needs its deterministic links."""
    missing_document_fingerprint = connection.execute(
        """
        SELECT 1
        FROM document_ir_blocks
        LEFT JOIN document_content_fingerprints
            ON document_content_fingerprints.document_id = document_ir_blocks.document_id
        WHERE document_content_fingerprints.document_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if missing_document_fingerprint is not None:
        return True
    missing_occurrence = connection.execute(
        """
        SELECT 1
        FROM evidence_refs
        LEFT JOIN evidence_occurrences
            ON evidence_occurrences.document_id = evidence_refs.document_id
            AND evidence_occurrences.block_id = evidence_refs.block_id
        WHERE evidence_occurrences.evidence_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if missing_occurrence is not None:
        return True
    return (
        connection.execute(
            """
            SELECT 1
            FROM evidence_occurrences
            LEFT JOIN evidence_fingerprints
                ON evidence_fingerprints.evidence_id = evidence_occurrences.evidence_id
            WHERE evidence_fingerprints.evidence_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        is not None
    )


def complete_reused_import_in(
    connection: sqlite3.Connection,
    *,
    state: _ImportState,
    document_id: str,
    completed_stage: str,
    skipped_stages: tuple[str, ...],
    deduplication: DesktopDeduplication,
    now: str,
    complete_job: CompleteJob,
) -> None:
    """Finish a D0/D1 import after recording exactly what was reused."""
    for stage in skipped_stages:
        stage_run_id = state.stage_ids[stage]
        connection.execute(
            """
            UPDATE stage_runs
            SET status = 'skipped', progress = 100, error_code = NULL,
                completed_at = COALESCE(completed_at, ?)
            WHERE stage_run_id = ? AND job_id = ?
            """,
            (now, stage_run_id, state.job_id),
        )
        connection.execute(
            """
            UPDATE stage_run_runtime
            SET status = 'skipped', error_code = NULL, updated_at = ?
            WHERE stage_run_id = ? AND job_id = ?
            """,
            (now, stage_run_id, state.job_id),
        )
    complete_job(connection, state.job_id, state.stage_ids[completed_stage], document_id, now)
    _complete_recovery_in(connection, state.recovery_run_id, now)
    record_import_deduplication_in(connection, state.job_id, deduplication, now)
    connection.execute("DELETE FROM quarantined_documents WHERE job_id = ?", (state.job_id,))


def publish_content_duplicate_in(
    connection: sqlite3.Connection,
    *,
    state: _ImportState,
    source: Path,
    document_id: str,
    asset_sha256: str,
    raw_path: str,
    raw_size: int,
    source_format: str,
    raw_media_type: str,
    source_images: tuple[SourceImage, ...],
    normalized_body_hash: str,
    canonical_document: DesktopImportedDocument,
    now: str,
    complete_job: CompleteJob,
) -> tuple[DesktopImportedDocument, bool]:
    """Persist a new source document that points at an exact existing body result."""
    existing_raw = _find_available_document_in(connection, asset_sha256)
    if existing_raw is not None:
        deduplication = DesktopDeduplication(
            level="D0",
            reason="raw_asset_sha256_match",
            reused_document_id=existing_raw.document_id,
            reused_evidence_count=0,
            reusable_stages=(
                "document_ir",
                "evidence",
                "deterministic_page_tree",
                "model_analysis",
                "search",
            ),
        )
        complete_reused_import_in(
            connection,
            state=state,
            document_id=existing_raw.document_id,
            completed_stage="document_ir",
            skipped_stages=(
                "evidence",
                "deterministic_page_tree",
                "model_analysis",
                "search",
            ),
            deduplication=deduplication,
            now=now,
            complete_job=complete_job,
        )
        return existing_raw, True

    canonical_row = connection.execute(
        """
        SELECT COALESCE(
            document_content_fingerprints.canonical_document_id,
            source_documents.document_id
        )
        FROM source_documents
        JOIN document_content_fingerprints
            ON document_content_fingerprints.document_id = source_documents.document_id
        WHERE source_documents.document_id = ?
            AND source_documents.availability = 'available'
            AND document_content_fingerprints.normalized_body_sha256 = ?
        """,
        (canonical_document.document_id, normalized_body_hash),
    ).fetchone()
    if canonical_row is None:
        raise RuntimeError("The matched normalized body is no longer available for reuse.")
    processing_document_id = str(canonical_row[0])

    _insert_raw_and_source_document(
        connection,
        document_id=document_id,
        source=source,
        asset_sha256=asset_sha256,
        raw_path=raw_path,
        raw_size=raw_size,
        source_format=source_format,
        raw_media_type=raw_media_type,
        now=now,
    )
    connection.execute(
        """
        INSERT INTO document_content_fingerprints (
            document_id, normalized_body_sha256, canonical_document_id, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (document_id, normalized_body_hash, processing_document_id, now),
    )
    connection.execute(
        """
        INSERT INTO evidence_occurrences (document_id, block_id, evidence_id, ordinal)
        SELECT ?, block_id, evidence_id, ordinal
        FROM evidence_occurrences
        WHERE document_id = ?
        ORDER BY ordinal
        """,
        (document_id, processing_document_id),
    )
    persist_source_images(
        connection,
        document_id=document_id,
        source_images=source_images,
        created_at=now,
    )
    deduplication = DesktopDeduplication(
        level="D1",
        reason="normalized_body_sha256_match",
        reused_document_id=canonical_document.document_id,
        reused_evidence_count=canonical_document.evidence_count,
        reusable_stages=("evidence", "model_analysis", "search"),
        normalized_body_sha256=normalized_body_hash,
    )
    complete_reused_import_in(
        connection,
        state=state,
        document_id=document_id,
        completed_stage="search",
        skipped_stages=("model_analysis",),
        deduplication=deduplication,
        now=now,
        complete_job=complete_job,
    )
    return (
        DesktopImportedDocument(
            document_id=document_id,
            name=source.name,
            source_format=source_format,
            raw_asset_sha256=asset_sha256,
            evidence_count=canonical_document.evidence_count,
            availability="available",
        ),
        True,
    )


def publish_document_in(
    connection: sqlite3.Connection,
    *,
    state: _ImportState,
    source: Path,
    document_id: str,
    asset_sha256: str,
    raw_path: str,
    raw_size: int,
    source_format: str,
    raw_media_type: str,
    blocks: tuple[DocumentIRBlock, ...],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    source_images: tuple[SourceImage, ...],
    normalized_body_hash: str,
    now: str,
    complete_job: CompleteJob,
) -> tuple[DesktopImportedDocument, bool]:
    """Atomically publish a unique body and share any D2 evidence fragments."""
    existing = _find_available_document_in(connection, asset_sha256)
    if existing is not None:
        complete_job(connection, state.job_id, state.stage_ids["search"], existing.document_id, now)
        _complete_recovery_in(connection, state.recovery_run_id, now)
        record_import_deduplication_in(
            connection,
            state.job_id,
            DesktopDeduplication(
                level="D0",
                reason="raw_asset_sha256_match",
                reused_document_id=existing.document_id,
                reused_evidence_count=0,
                reusable_stages=(),
            ),
            now,
        )
        connection.execute("DELETE FROM quarantined_documents WHERE job_id = ?", (state.job_id,))
        return existing, True

    _insert_raw_and_source_document(
        connection,
        document_id=document_id,
        source=source,
        asset_sha256=asset_sha256,
        raw_path=raw_path,
        raw_size=raw_size,
        source_format=source_format,
        raw_media_type=raw_media_type,
        now=now,
    )
    connection.execute(
        """
        INSERT INTO document_content_fingerprints (
            document_id, normalized_body_sha256, canonical_document_id, created_at
        ) VALUES (?, ?, NULL, ?)
        """,
        (document_id, normalized_body_hash, now),
    )
    connection.executemany(
        """
        INSERT INTO document_ir_blocks (
            block_id, document_id, ordinal, kind, text, heading_path, locator_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                block.block_id,
                document_id,
                block.ordinal,
                block.kind,
                block.text,
                json.dumps(block.heading_path, ensure_ascii=False),
                _block_locator(block),
            )
            for block in blocks
        ],
    )

    reused_evidence_count = 0
    for evidence_id, block in evidence:
        fingerprint = evidence_sha256(block)
        row = connection.execute(
            "SELECT evidence_id FROM evidence_fingerprints WHERE evidence_sha256 = ?",
            (fingerprint,),
        ).fetchone()
        canonical_evidence_id = str(row[0]) if row is not None else evidence_id
        if row is None:
            connection.execute(
                """
                INSERT INTO evidence_refs (
                    evidence_id, document_id, block_id, ordinal, text, locator_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    document_id,
                    block.block_id,
                    block.ordinal,
                    block.text,
                    _block_locator(block),
                ),
            )
            connection.execute(
                "INSERT INTO evidence_fts (evidence_id, document_id, content) VALUES (?, ?, ?)",
                (evidence_id, document_id, block.text),
            )
            connection.execute(
                """
                INSERT INTO evidence_fingerprints (evidence_sha256, evidence_id, created_at)
                VALUES (?, ?, ?)
                """,
                (fingerprint, evidence_id, now),
            )
        else:
            reused_evidence_count += 1
        connection.execute(
            """
            INSERT INTO evidence_occurrences (document_id, block_id, evidence_id, ordinal)
            VALUES (?, ?, ?, ?)
            """,
            (document_id, block.block_id, canonical_evidence_id, block.ordinal),
        )

    persist_source_images(
        connection,
        document_id=document_id,
        source_images=source_images,
        created_at=now,
    )
    complete_job(connection, state.job_id, state.stage_ids["search"], document_id, now)
    _complete_recovery_in(connection, state.recovery_run_id, now)
    if reused_evidence_count:
        record_import_deduplication_in(
            connection,
            state.job_id,
            DesktopDeduplication(
                level="D2",
                reason="evidence_sha256_match",
                reused_document_id=None,
                reused_evidence_count=reused_evidence_count,
                reusable_stages=("evidence",),
            ),
            now,
        )
    connection.execute("DELETE FROM quarantined_documents WHERE job_id = ?", (state.job_id,))
    return (
        DesktopImportedDocument(
            document_id=document_id,
            name=source.name,
            source_format=source_format,
            raw_asset_sha256=asset_sha256,
            evidence_count=len(evidence),
            availability="available",
        ),
        bool(reused_evidence_count),
    )


def record_import_deduplication_in(
    connection: sqlite3.Connection,
    job_id: str,
    deduplication: DesktopDeduplication,
    now: str,
) -> None:
    """Persist a bridge-safe explanation instead of inferring reuse from skipped stages."""
    connection.execute(
        """
        INSERT INTO import_deduplications (
            job_id, level, reason, reused_document_id, reused_evidence_count,
            normalized_body_sha256, reusable_stages_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            level = excluded.level,
            reason = excluded.reason,
            reused_document_id = excluded.reused_document_id,
            reused_evidence_count = excluded.reused_evidence_count,
            normalized_body_sha256 = excluded.normalized_body_sha256,
            reusable_stages_json = excluded.reusable_stages_json,
            created_at = excluded.created_at
        """,
        (
            job_id,
            deduplication.level,
            deduplication.reason,
            deduplication.reused_document_id,
            deduplication.reused_evidence_count,
            deduplication.normalized_body_sha256,
            json.dumps(deduplication.reusable_stages),
            now,
        ),
    )


def _insert_raw_and_source_document(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    source: Path,
    asset_sha256: str,
    raw_path: str,
    raw_size: int,
    source_format: str,
    raw_media_type: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO raw_assets (
            asset_sha256, byte_size, media_type, raw_path, original_name, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_sha256) DO NOTHING
        """,
        (asset_sha256, raw_size, raw_media_type, raw_path, source.name, now),
    )
    connection.execute(
        """
        INSERT INTO source_documents (
            document_id, asset_sha256, display_name, source_format, availability,
            created_at, available_at
        ) VALUES (?, ?, ?, ?, 'available', ?, ?)
        """,
        (document_id, asset_sha256, source.name, source_format, now, now),
    )


def _complete_recovery_in(
    connection: sqlite3.Connection, recovery_run_id: str | None, now: str
) -> None:
    if recovery_run_id is None:
        return
    connection.execute(
        """
        UPDATE recovery_runs
        SET status = 'completed', completed_at = ?
        WHERE recovery_run_id = ?
        """,
        (now, recovery_run_id),
    )


def _find_available_document_in(
    connection: sqlite3.Connection, asset_sha256: str
) -> DesktopImportedDocument | None:
    """Avoid a workspace-import cycle while sharing the normal D0 projection."""
    from openkb.desktop_import_queries import find_available_document_in

    return find_available_document_in(connection, asset_sha256)


def _normalized_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()


def _sha256(payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _heading_path(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("Desktop document heading path is not valid JSON.") from error
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ValueError("Desktop document heading path has an invalid shape.")
    return tuple(decoded)


def _block_locator(block: DocumentIRBlock) -> str:
    locator = block.locator or {"line_start": block.line_start, "line_end": block.line_end}
    return json.dumps(locator, ensure_ascii=False)
