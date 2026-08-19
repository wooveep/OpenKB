"""SQLite ownership and background rebuilds for deterministic Document PageTrees."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_import_artifacts import (
    DocumentIRBlock,
    SourceImage,
    document_ir_from_checkpoint,
    evidence_from_checkpoint,
    source_images_from_checkpoint,
)
from openkb.desktop_page_tree import (
    PAGE_TREE_FAILURE_CODE,
    PageTreeEvidenceBinding,
    PageTreeGeneration,
    PageTreeImageBinding,
    PageTreeNode,
    PageTreeStageOutcome,
    build_deterministic_page_tree,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

logger = logging.getLogger(__name__)
_REBUILD_WORKER_LOCK = threading.Lock()
_REBUILD_START_LOCK = threading.Lock()
_ACTIVE_REBUILD_WORKERS: set[Path] = set()
_REBUILD_RERUN_REQUESTS: set[Path] = set()


def publish_or_queue_page_tree_in(
    connection: sqlite3.Connection,
    document_id: str,
    outcome: PageTreeStageOutcome,
) -> None:
    """Publish a valid generation in the document transaction or queue a rebuild."""
    if outcome.generation is None:
        queue_page_tree_rebuild_in(
            connection,
            document_id,
            reason="import_build_failed",
            error_code=outcome.error_code or PAGE_TREE_FAILURE_CODE,
        )
        return
    connection.execute("SAVEPOINT publish_document_page_tree")
    try:
        persist_page_tree_generation_in(connection, document_id, outcome.generation)
        connection.execute("RELEASE SAVEPOINT publish_document_page_tree")
        return
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT publish_document_page_tree")
        connection.execute("RELEASE SAVEPOINT publish_document_page_tree")
        logger.exception("Could not publish deterministic PageTree for %s", document_id)
        queue_page_tree_rebuild_in(
            connection,
            document_id,
            reason="publication_failed",
            error_code=PAGE_TREE_FAILURE_CODE,
        )
        return


def persist_page_tree_generation_in(
    connection: sqlite3.Connection,
    document_id: str,
    generation: PageTreeGeneration,
) -> None:
    """Persist immutable nodes and activate the generation for one Available document."""
    if generation.document_version_id != document_id or generation.status != "ready":
        raise ValueError("Document PageTree generation is bound to another version.")
    available = connection.execute(
        "SELECT 1 FROM source_documents WHERE document_id = ? AND availability = 'available'",
        (document_id,),
    ).fetchone()
    if available is None:
        raise ValueError("Document PageTree requires an Available document version.")
    evidence_by_ordinal = {
        int(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT ordinal, evidence_id FROM evidence_occurrences "
            "WHERE document_id = ? ORDER BY ordinal",
            (document_id,),
        ).fetchall()
    }
    images_by_ordinal = {
        int(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT ordinal, source_image_id FROM source_images "
            "WHERE document_id = ? ORDER BY ordinal",
            (document_id,),
        ).fetchall()
    }
    for node in generation.nodes:
        if any(binding.block_ordinal not in evidence_by_ordinal for binding in node.evidence):
            raise ValueError("Document PageTree Evidence occurrence is missing.")
        if any(binding.image_ordinal not in images_by_ordinal for binding in node.source_images):
            raise ValueError("Document PageTree Source Image occurrence is missing.")

    existing = connection.execute(
        "SELECT document_id FROM document_page_tree_generations WHERE generation_id = ?",
        (generation.generation_id,),
    ).fetchone()
    if existing is None:
        _insert_generation_in(
            connection,
            document_id,
            generation,
            evidence_by_ordinal,
            images_by_ordinal,
        )
    elif str(existing[0]) != document_id:
        raise ValueError("Document PageTree generation identity collides with another document.")

    previous = connection.execute(
        "SELECT generation_id FROM document_page_tree_current WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if previous is not None and str(previous[0]) != generation.generation_id:
        connection.execute(
            "UPDATE document_page_tree_generations SET status = 'superseded' "
            "WHERE generation_id = ?",
            (str(previous[0]),),
        )
    connection.execute(
        "UPDATE document_page_tree_generations SET status = 'current' WHERE generation_id = ?",
        (generation.generation_id,),
    )
    now = _timestamp()
    connection.execute(
        """
        INSERT INTO document_page_tree_current (document_id, generation_id, activated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            generation_id = excluded.generation_id,
            activated_at = excluded.activated_at
        """,
        (document_id, generation.generation_id, now),
    )
    connection.execute(
        """
        UPDATE document_page_tree_rebuild_tasks
        SET status = 'completed', error_code = NULL, updated_at = ?, completed_at = ?
        WHERE document_id = ?
        """,
        (now, now, document_id),
    )


def _insert_generation_in(
    connection: sqlite3.Connection,
    document_id: str,
    generation: PageTreeGeneration,
    evidence_by_ordinal: dict[int, str],
    images_by_ordinal: dict[int, str],
) -> None:
    connection.execute(
        """
        INSERT INTO document_page_tree_generations (
            generation_id, document_id, provider_kind, provider_version,
            structural_ir_fingerprint, locator_mapping_digest, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'current', ?)
        """,
        (
            generation.generation_id,
            document_id,
            generation.provider_kind,
            generation.provider_version,
            generation.structural_ir_fingerprint,
            generation.locator_mapping_digest,
            generation.created_at,
        ),
    )
    for node in generation.nodes:
        connection.execute(
            """
            INSERT INTO document_page_tree_nodes (
                generation_id, node_id, parent_node_id, node_order, depth,
                kind, title, summary, locator_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation.generation_id,
                node.node_id,
                node.parent_node_id,
                node.order,
                node.depth,
                node.kind,
                node.title,
                node.summary,
                _json(node.locator),
            ),
        )
        for order, binding in enumerate(node.evidence):
            connection.execute(
                """
                INSERT INTO document_page_tree_node_evidence (
                    generation_id, node_id, evidence_id, block_ordinal, association_order
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    generation.generation_id,
                    node.node_id,
                    evidence_by_ordinal[binding.block_ordinal],
                    binding.block_ordinal,
                    order,
                ),
            )
        for order, image_binding in enumerate(node.source_images):
            connection.execute(
                """
                INSERT INTO document_page_tree_node_images (
                    generation_id, node_id, source_image_id, image_ordinal, association_order
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    generation.generation_id,
                    node.node_id,
                    images_by_ordinal[image_binding.image_ordinal],
                    image_binding.image_ordinal,
                    order,
                ),
            )


def queue_page_tree_rebuild_in(
    connection: sqlite3.Connection,
    document_id: str,
    *,
    reason: str,
    error_code: str,
) -> None:
    now = _timestamp()
    connection.execute(
        """
        INSERT INTO document_page_tree_rebuild_tasks (
            document_id, status, reason, error_code, attempt_count,
            created_at, updated_at, completed_at
        ) VALUES (?, 'pending', ?, ?, 0, ?, ?, NULL)
        ON CONFLICT(document_id) DO UPDATE SET
            status = 'pending', reason = excluded.reason, error_code = excluded.error_code,
            updated_at = excluded.updated_at, completed_at = NULL
        """,
        (document_id, reason, error_code, now, now),
    )


def load_current_page_tree_in(
    connection: sqlite3.Connection, document_id: str
) -> PageTreeGeneration | None:
    row = connection.execute(
        """
        SELECT generations.generation_id, generations.provider_kind,
            generations.provider_version, generations.structural_ir_fingerprint,
            generations.locator_mapping_digest, generations.created_at, generations.status
        FROM document_page_tree_current AS current
        JOIN document_page_tree_generations AS generations
            ON generations.generation_id = current.generation_id
        WHERE current.document_id = ?
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    generation_id = str(row[0])
    node_rows = connection.execute(
        """
        SELECT node_id, parent_node_id, node_order, depth, kind, title, summary, locator_json
        FROM document_page_tree_nodes WHERE generation_id = ? ORDER BY node_order
        """,
        (generation_id,),
    ).fetchall()
    evidence_by_node = _evidence_bindings_in(connection, generation_id)
    images_by_node = _image_bindings_in(connection, generation_id)
    nodes = tuple(
        PageTreeNode(
            node_id=str(node[0]),
            parent_node_id=str(node[1]) if node[1] is not None else None,
            order=int(node[2]),
            depth=int(node[3]),
            kind=str(node[4]),
            title=str(node[5]),
            summary=str(node[6]) if node[6] is not None else None,
            locator=_json_object(node[7]),
            evidence=tuple(evidence_by_node.get(str(node[0]), ())),
            source_images=tuple(images_by_node.get(str(node[0]), ())),
        )
        for node in node_rows
    )
    return PageTreeGeneration(
        generation_id=generation_id,
        document_version_id=document_id,
        provider_kind=str(row[1]),
        provider_version=str(row[2]),
        structural_ir_fingerprint=str(row[3]),
        locator_mapping_digest=str(row[4]),
        created_at=str(row[5]),
        status="ready" if str(row[6]) == "current" else str(row[6]),
        nodes=nodes,
    )


def _evidence_bindings_in(
    connection: sqlite3.Connection, generation_id: str
) -> dict[str, list[PageTreeEvidenceBinding]]:
    rows = connection.execute(
        """
        SELECT node_id, evidence_id, block_ordinal
        FROM document_page_tree_node_evidence
        WHERE generation_id = ? ORDER BY node_id, association_order
        """,
        (generation_id,),
    ).fetchall()
    values: dict[str, list[PageTreeEvidenceBinding]] = {}
    for node_id, evidence_id, ordinal in rows:
        values.setdefault(str(node_id), []).append(
            PageTreeEvidenceBinding(str(evidence_id), int(ordinal))
        )
    return values


def _image_bindings_in(
    connection: sqlite3.Connection, generation_id: str
) -> dict[str, list[PageTreeImageBinding]]:
    rows = connection.execute(
        """
        SELECT node_id, source_image_id, image_ordinal
        FROM document_page_tree_node_images
        WHERE generation_id = ? ORDER BY node_id, association_order
        """,
        (generation_id,),
    ).fetchall()
    values: dict[str, list[PageTreeImageBinding]] = {}
    for node_id, image_id, ordinal in rows:
        values.setdefault(str(node_id), []).append(
            PageTreeImageBinding(str(image_id), int(ordinal))
        )
    return values


def rebuild_pending_page_trees(kb_dir: Path) -> None:
    """Attempt each persisted rebuild once without changing document availability."""
    resolved = kb_dir.expanduser().resolve()
    state_dir = desktop_state_dir(resolved)
    database_path = desktop_state_database_path(resolved)
    with _REBUILD_WORKER_LOCK:
        connection = _connect(database_path)
        try:
            rows = connection.execute(
                """
                SELECT document_id FROM document_page_tree_rebuild_tasks
                WHERE status IN ('pending', 'running', 'failed') ORDER BY updated_at, document_id
                """
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            _rebuild_one(state_dir, database_path, str(row[0]))


def _rebuild_one(state_dir: Path, database_path: Path, document_id: str) -> None:
    try:
        with kb_ingest_lock(state_dir):
            connection = _connect(database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE document_page_tree_rebuild_tasks
                    SET status = 'running', attempt_count = attempt_count + 1,
                        error_code = NULL, updated_at = ?, completed_at = NULL
                    WHERE document_id = ? AND status IN ('pending', 'running', 'failed')
                    """,
                    (_timestamp(), document_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return
                blocks, evidence, images = _published_page_tree_input_in(connection, document_id)
                generation = build_deterministic_page_tree(document_id, blocks, evidence, images)
                persist_page_tree_generation_in(connection, document_id, generation)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
    except Exception:
        logger.exception("Document PageTree rebuild failed for %s", document_id)
        _mark_rebuild_failed(state_dir, database_path, document_id)


def start_page_tree_rebuilds(kb_dir: Path) -> None:
    """Start one daemon pass over queued deterministic rebuilds."""
    resolved = kb_dir.expanduser().resolve()
    try:
        connection = _connect(desktop_state_database_path(resolved))
        try:
            pending = connection.execute(
                "SELECT 1 FROM document_page_tree_rebuild_tasks "
                "WHERE status IN ('pending', 'running', 'failed') LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        logger.warning("Could not inspect deterministic PageTree rebuild work.")
        return
    if pending is None:
        return
    with _REBUILD_START_LOCK:
        if resolved in _ACTIVE_REBUILD_WORKERS:
            _REBUILD_RERUN_REQUESTS.add(resolved)
            return
        _ACTIVE_REBUILD_WORKERS.add(resolved)
    try:
        threading.Thread(
            target=_run_page_tree_rebuild_worker,
            args=(resolved,),
            daemon=True,
            name="openkb-page-tree-rebuild",
        ).start()
    except RuntimeError:
        with _REBUILD_START_LOCK:
            _ACTIVE_REBUILD_WORKERS.discard(resolved)
            _REBUILD_RERUN_REQUESTS.discard(resolved)
        logger.warning("Could not start deterministic PageTree rebuild worker.")


def _run_page_tree_rebuild_worker(kb_dir: Path) -> None:
    try:
        while True:
            rebuild_pending_page_trees(kb_dir)
            with _REBUILD_START_LOCK:
                if kb_dir in _REBUILD_RERUN_REQUESTS:
                    _REBUILD_RERUN_REQUESTS.discard(kb_dir)
                    continue
                _ACTIVE_REBUILD_WORKERS.discard(kb_dir)
                return
    except Exception:
        logger.exception("Deterministic PageTree rebuild worker stopped unexpectedly.")
    finally:
        with _REBUILD_START_LOCK:
            _ACTIVE_REBUILD_WORKERS.discard(kb_dir)
            _REBUILD_RERUN_REQUESTS.discard(kb_dir)


def _published_page_tree_input_in(
    connection: sqlite3.Connection, document_id: str
) -> tuple[
    tuple[DocumentIRBlock, ...],
    tuple[tuple[str, DocumentIRBlock], ...],
    tuple[SourceImage, ...],
]:
    checkpoint_input = _checkpoint_page_tree_input_in(connection, document_id)
    if checkpoint_input is not None:
        return checkpoint_input
    row = connection.execute(
        """
        SELECT documents.document_id, fingerprints.canonical_document_id
        FROM source_documents AS documents
        LEFT JOIN document_content_fingerprints AS fingerprints
            ON fingerprints.document_id = documents.document_id
        WHERE documents.document_id = ? AND documents.availability = 'available'
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Document PageTree rebuild requires an Available document.")
    if row[1] is not None and str(row[1]) != document_id:
        raise ValueError("A D1 Document PageTree cannot reuse unverified canonical locators.")
    processing_document_id = str(row[1]) if row[1] is not None else str(row[0])
    block_rows = connection.execute(
        """
        SELECT block_id, ordinal, kind, text, heading_path, locator_json
        FROM document_ir_blocks WHERE document_id = ? ORDER BY ordinal
        """,
        (processing_document_id,),
    ).fetchall()
    blocks = tuple(_block_from_row(value) for value in block_rows)
    by_ordinal = {block.ordinal: block for block in blocks}
    evidence_rows = connection.execute(
        "SELECT evidence_id, ordinal FROM evidence_occurrences "
        "WHERE document_id = ? ORDER BY ordinal",
        (document_id,),
    ).fetchall()
    evidence = tuple((str(value[0]), by_ordinal[_integer(value[1])]) for value in evidence_rows)
    image_rows = connection.execute(
        """
        SELECT source_image_id, ordinal, image_sha256, byte_size, media_type,
            display_name, alt_text, locator_json
        FROM source_images WHERE document_id = ? ORDER BY ordinal
        """,
        (document_id,),
    ).fetchall()
    return blocks, evidence, tuple(_image_from_row(value) for value in image_rows)


def _checkpoint_page_tree_input_in(
    connection: sqlite3.Connection, document_id: str
) -> (
    tuple[
        tuple[DocumentIRBlock, ...],
        tuple[tuple[str, DocumentIRBlock], ...],
        tuple[SourceImage, ...],
    ]
    | None
):
    row = connection.execute(
        """
        SELECT ir_runtime.checkpoint_json, evidence_runtime.checkpoint_json
        FROM import_jobs AS jobs
        JOIN stage_runs AS ir_stage
            ON ir_stage.job_id = jobs.job_id AND ir_stage.stage = 'document_ir'
        JOIN stage_run_runtime AS ir_runtime
            ON ir_runtime.stage_run_id = ir_stage.stage_run_id
        LEFT JOIN stage_runs AS evidence_stage
            ON evidence_stage.job_id = jobs.job_id AND evidence_stage.stage = 'evidence'
        LEFT JOIN stage_run_runtime AS evidence_runtime
            ON evidence_runtime.stage_run_id = evidence_stage.stage_run_id
        WHERE jobs.document_id = ?
            AND ir_runtime.checkpoint_json IS NOT NULL
        ORDER BY jobs.completed_at DESC, jobs.created_at DESC LIMIT 1
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    document_ir = json.loads(str(row[0]))
    blocks = document_ir_from_checkpoint(document_ir)
    if row[1] is not None:
        evidence = evidence_from_checkpoint(json.loads(str(row[1])), blocks)
    else:
        block_by_ordinal = {block.ordinal: block for block in blocks}
        evidence_rows = connection.execute(
            "SELECT evidence_id, ordinal FROM evidence_occurrences "
            "WHERE document_id = ? ORDER BY ordinal",
            (document_id,),
        ).fetchall()
        if len(evidence_rows) != len(blocks) or any(
            _integer(value[1]) not in block_by_ordinal for value in evidence_rows
        ):
            raise ValueError("Document PageTree Evidence occurrences do not match its IR.")
        evidence = tuple(
            (str(value[0]), block_by_ordinal[_integer(value[1])]) for value in evidence_rows
        )
    return (
        blocks,
        evidence,
        source_images_from_checkpoint(document_ir),
    )


def _block_from_row(value: tuple[object, ...]) -> DocumentIRBlock:
    return DocumentIRBlock(
        block_id=str(value[0]),
        ordinal=_integer(value[1]),
        kind=str(value[2]),
        text=str(value[3]),
        heading_path=_json_strings(value[4]),
        line_start=1,
        line_end=1,
        locator=_json_object(value[5]),
    )


def _image_from_row(value: tuple[object, ...]) -> SourceImage:
    filename = str(value[5])
    return SourceImage(
        image_id=str(value[0]),
        ordinal=_integer(value[1]),
        image_sha256=str(value[2]),
        byte_size=_integer(value[3]),
        media_type=str(value[4]),
        filename=filename,
        extension=Path(filename).suffix or ".bin",
        alt_text=str(value[6]) if value[6] is not None else None,
        locator=_json_object(value[7]),
    )


def _mark_rebuild_failed(state_dir: Path, database_path: Path, document_id: str) -> None:
    try:
        with kb_ingest_lock(state_dir):
            connection = _connect(database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE document_page_tree_rebuild_tasks
                        SET status = 'failed', error_code = ?, updated_at = ?
                        WHERE document_id = ?
                        """,
                        (PAGE_TREE_FAILURE_CODE, _timestamp(), document_id),
                    )
            finally:
                connection.close()
    except (OSError, sqlite3.Error):
        logger.warning("Could not record PageTree rebuild failure for %s", document_id)


def _json_strings(value: object) -> tuple[str, ...]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("Document PageTree heading path is invalid.")
    return tuple(parsed)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Document PageTree integer field is invalid.")
    return value


def _json_object(value: object) -> dict[str, object]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("Document PageTree locator is invalid.")
    return parsed


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
