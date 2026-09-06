"""SQLite ownership and background rebuilds for deterministic Document PageTrees."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from openkb.importing.artifacts import (
    DocumentIRBlock,
    SourceImage,
    document_ir_from_checkpoint,
    evidence_from_checkpoint,
    source_images_from_checkpoint,
)
from openkb.locks import kb_ingest_lock
from openkb.page_tree.bindings import load_page_tree_bindings_in
from openkb.page_tree.enrichment import active_page_tree_summaries_in
from openkb.page_tree.rebuild_state import (
    claim_page_tree_rebuild,
    mark_page_tree_rebuild_failed,
    queue_page_tree_rebuild_in,
    ready_page_tree_rebuild_document_ids_in,
    rebuild_claim_is_current_in,
)
from openkb.page_tree.reuse import (
    PageTreeCanonicalDependencyError,
    require_d1_canonical_provider_in,
    reuse_matching_d1_generation_in,
)
from openkb.page_tree.tree import (
    DETERMINISTIC_PROVIDER_KIND,
    DETERMINISTIC_PROVIDER_VERSION,
    PAGE_TREE_FAILURE_CODE,
    PageTreeGeneration,
    PageTreeNode,
    PageTreeStageOutcome,
    build_deterministic_page_tree,
)
from openkb.page_tree.validation import validate_current_page_tree
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

logger = logging.getLogger(__name__)
_REBUILD_WORKER_LOCK = threading.Lock()
_REBUILD_START_LOCK = threading.Lock()
_ACTIVE_REBUILD_WORKERS: set[Path] = set()
_REBUILD_RERUN_REQUESTS: set[Path] = set()
_GENERATION_LEASE_LOCK = threading.Lock()
_GENERATION_LEASES: dict[str, int] = {}


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
    except PageTreeCanonicalDependencyError as error:
        connection.execute("ROLLBACK TO SAVEPOINT publish_document_page_tree")
        connection.execute("RELEASE SAVEPOINT publish_document_page_tree")
        queue_page_tree_rebuild_in(
            connection,
            error.canonical_document_id,
            reason="provider_update",
            error_code=PAGE_TREE_FAILURE_CODE,
            provider_kind=outcome.generation.provider_kind,
            provider_version=outcome.generation.provider_version,
        )
        queue_page_tree_rebuild_in(
            connection,
            document_id,
            reason="canonical_dependency",
            error_code=PAGE_TREE_FAILURE_CODE,
            provider_kind=outcome.generation.provider_kind,
            provider_version=outcome.generation.provider_version,
        )
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
    require_d1_canonical_provider_in(connection, document_id, generation)
    generation = reuse_matching_d1_generation_in(connection, document_id, generation)
    store_page_tree_generation_in(connection, document_id, generation)

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
    _delete_unleased_superseded_in(connection, document_id)
    connection.execute(
        """
        UPDATE document_page_tree_rebuild_tasks
        SET status = 'completed', error_code = NULL, updated_at = ?, completed_at = ?
        WHERE document_id = ?
        """,
        (now, now, document_id),
    )


def store_page_tree_generation_in(
    connection: sqlite3.Connection,
    document_id: str,
    generation: PageTreeGeneration,
) -> None:
    """Store one validated generation without selecting which provider is active."""
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
            structural_ir_fingerprint, locator_mapping_digest, status, created_at,
            reused_from_generation_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'current', ?, ?)
        """,
        (
            generation.generation_id,
            document_id,
            generation.provider_kind,
            generation.provider_version,
            generation.structural_ir_fingerprint,
            generation.locator_mapping_digest,
            generation.created_at,
            generation.reused_from_generation_id,
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


def load_current_page_tree_in(
    connection: sqlite3.Connection, document_id: str
) -> PageTreeGeneration | None:
    row = connection.execute(
        """
        SELECT generation_id FROM document_page_tree_current WHERE document_id = ?
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    return load_page_tree_generation_in(connection, document_id, str(row[0]))


def load_page_tree_generation_in(
    connection: sqlite3.Connection,
    document_id: str,
    generation_id: str,
    *,
    require_current: bool = True,
) -> PageTreeGeneration:
    """Load one immutable provider generation through the OpenKB PageTree contract."""
    row = connection.execute(
        """
        SELECT provider_kind, provider_version, structural_ir_fingerprint,
            locator_mapping_digest, created_at, status, reused_from_generation_id
        FROM document_page_tree_generations
        WHERE document_id = ? AND generation_id = ?
        """,
        (document_id, generation_id),
    ).fetchone()
    if row is None or (require_current and str(row[5]) != "current"):
        raise ValueError("The current Document PageTree generation is not current.")
    node_rows = connection.execute(
        """
        SELECT node_id, parent_node_id, node_order, depth, kind, title, summary, locator_json
        FROM document_page_tree_nodes WHERE generation_id = ? ORDER BY node_order
        """,
        (generation_id,),
    ).fetchall()
    evidence_by_node, images_by_node = load_page_tree_bindings_in(connection, generation_id)
    enriched_summaries = active_page_tree_summaries_in(connection, document_id, generation_id)
    nodes = tuple(
        PageTreeNode(
            node_id=str(node[0]),
            parent_node_id=str(node[1]) if node[1] is not None else None,
            order=int(node[2]),
            depth=int(node[3]),
            kind=str(node[4]),
            title=str(node[5]),
            summary=enriched_summaries.get(
                str(node[0]), str(node[6]) if node[6] is not None else None
            ),
            locator=_json_object(node[7]),
            evidence=tuple(evidence_by_node.get(str(node[0]), ())),
            source_images=tuple(images_by_node.get(str(node[0]), ())),
        )
        for node in node_rows
    )
    generation = PageTreeGeneration(
        generation_id=generation_id,
        document_version_id=document_id,
        provider_kind=str(row[0]),
        provider_version=str(row[1]),
        structural_ir_fingerprint=str(row[2]),
        locator_mapping_digest=str(row[3]),
        created_at=str(row[4]),
        status="ready",
        nodes=nodes,
        reused_from_generation_id=str(row[6]) if row[6] is not None else None,
    )
    validate_current_page_tree(generation)
    return generation


@contextmanager
def lease_current_page_tree(kb_dir: Path, document_id: str) -> Iterator[PageTreeGeneration | None]:
    """Keep a request's current immutable generation until its work finishes."""
    with _lease_page_tree_generation(kb_dir, document_id, generation_id=None) as generation:
        yield generation


@contextmanager
def lease_page_tree_generation(
    kb_dir: Path, document_id: str, generation_id: str
) -> Iterator[PageTreeGeneration | None]:
    """Keep one snapshot-selected immutable PageTree generation until work finishes."""
    if not generation_id:
        yield None
        return
    with _lease_page_tree_generation(
        kb_dir, document_id, generation_id=generation_id
    ) as generation:
        yield generation


@contextmanager
def _lease_page_tree_generation(
    kb_dir: Path, document_id: str, *, generation_id: str | None
) -> Iterator[PageTreeGeneration | None]:
    resolved = kb_dir.expanduser().resolve()
    state_dir = desktop_state_dir(resolved)
    generation: PageTreeGeneration | None = None
    with kb_ingest_lock(state_dir):
        connection = _connect(desktop_state_database_path(resolved))
        try:
            generation = (
                load_current_page_tree_in(connection, document_id)
                if generation_id is None
                else load_page_tree_generation_in(
                    connection,
                    document_id,
                    generation_id,
                    require_current=False,
                )
            )
            if generation is not None:
                with _GENERATION_LEASE_LOCK:
                    generation_id = generation.generation_id
                    _GENERATION_LEASES[generation_id] = _GENERATION_LEASES.get(generation_id, 0) + 1
        finally:
            connection.close()
    try:
        yield generation
    finally:
        if generation is not None:
            with _GENERATION_LEASE_LOCK:
                remaining = _GENERATION_LEASES[generation.generation_id] - 1
                if remaining:
                    _GENERATION_LEASES[generation.generation_id] = remaining
                else:
                    del _GENERATION_LEASES[generation.generation_id]
            cleanup_page_tree_generations(resolved, document_id)


def cleanup_page_tree_generations(kb_dir: Path, document_id: str) -> None:
    """Remove superseded trees once no active request leases them."""
    resolved = kb_dir.expanduser().resolve()
    try:
        with kb_ingest_lock(desktop_state_dir(resolved)):
            connection = _connect(desktop_state_database_path(resolved))
            try:
                with connection:
                    _delete_unleased_superseded_in(connection, document_id)
            finally:
                connection.close()
    except (OSError, sqlite3.Error):
        logger.warning("Could not clean superseded PageTree generations for %s", document_id)


def _delete_unleased_superseded_in(connection: sqlite3.Connection, document_id: str) -> None:
    generation_ids = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT generation_id FROM document_page_tree_generations "
            "WHERE document_id = ? AND status = 'superseded'",
            (document_id,),
        ).fetchall()
    )
    with _GENERATION_LEASE_LOCK:
        deletable = tuple(value for value in generation_ids if value not in _GENERATION_LEASES)
    connection.executemany(
        "DELETE FROM document_page_tree_generations WHERE generation_id = ?",
        ((value,) for value in deletable),
    )


def rebuild_pending_page_trees(kb_dir: Path) -> None:
    """Attempt each persisted rebuild once without changing document availability."""
    resolved = kb_dir.expanduser().resolve()
    state_dir = desktop_state_dir(resolved)
    database_path = desktop_state_database_path(resolved)
    with _REBUILD_WORKER_LOCK:
        attempted: set[str] = set()
        while True:
            connection = _connect(database_path)
            try:
                document_ids = tuple(
                    document_id
                    for document_id in ready_page_tree_rebuild_document_ids_in(connection)
                    if document_id not in attempted
                )
            finally:
                connection.close()
            if not document_ids:
                return
            for document_id in document_ids:
                attempted.add(document_id)
                _rebuild_one(state_dir, database_path, document_id)


def _rebuild_one(state_dir: Path, database_path: Path, document_id: str) -> None:
    claim = claim_page_tree_rebuild(state_dir, database_path, document_id)
    if claim is None:
        return
    try:
        with kb_ingest_lock(state_dir):
            connection = _connect(database_path)
            try:
                blocks, evidence, images = published_page_tree_input_in(connection, document_id)
            finally:
                connection.close()
        generation = build_deterministic_page_tree(
            document_id,
            blocks,
            evidence,
            images,
            provider_kind=claim.provider_kind,
            provider_version=claim.provider_version,
        )
        with kb_ingest_lock(state_dir):
            connection = _connect(database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not rebuild_claim_is_current_in(connection, claim):
                    connection.rollback()
                    return
                persist_page_tree_generation_in(connection, document_id, generation)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
    except Exception:
        logger.exception("Document PageTree rebuild failed for %s", document_id)
        try:
            mark_page_tree_rebuild_failed(state_dir, database_path, claim, PAGE_TREE_FAILURE_CODE)
        except (OSError, sqlite3.Error):
            logger.warning("Could not record PageTree rebuild failure for %s", document_id)


def start_page_tree_rebuilds(kb_dir: Path) -> None:
    """Start one daemon pass over queued deterministic rebuilds."""
    resolved = kb_dir.expanduser().resolve()
    try:
        _ensure_page_tree_rebuilds(resolved)
        connection = _connect(desktop_state_database_path(resolved))
        try:
            pending = connection.execute(
                "SELECT 1 FROM document_page_tree_rebuild_tasks "
                "WHERE status IN ('pending', 'running', 'failed') LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
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


def _ensure_page_tree_rebuilds(kb_dir: Path) -> None:
    state_dir = desktop_state_dir(kb_dir)
    with kb_ingest_lock(state_dir):
        connection = _connect(desktop_state_database_path(kb_dir))
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT documents.document_id, current.generation_id
                FROM source_documents AS documents
                LEFT JOIN document_page_tree_current AS current
                    ON current.document_id = documents.document_id
                LEFT JOIN document_page_tree_generations AS generations
                    ON generations.generation_id = current.generation_id
                WHERE documents.availability = 'available' AND (
                    current.generation_id IS NULL
                    OR generations.provider_kind != ? OR generations.provider_version != ?
                )
                """,
                (DETERMINISTIC_PROVIDER_KIND, DETERMINISTIC_PROVIDER_VERSION),
            ).fetchall()
            for document_id, generation_id in rows:
                queue_page_tree_rebuild_in(
                    connection,
                    str(document_id),
                    reason="missing_generation" if generation_id is None else "provider_update",
                    error_code=PAGE_TREE_FAILURE_CODE,
                    provider_kind=DETERMINISTIC_PROVIDER_KIND,
                    provider_version=DETERMINISTIC_PROVIDER_VERSION,
                )
            for (document_id,) in connection.execute(
                "SELECT DISTINCT document_id FROM document_page_tree_generations "
                "WHERE status = 'superseded'"
            ).fetchall():
                _delete_unleased_superseded_in(connection, str(document_id))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


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


def published_page_tree_input_in(
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
        evidence_from_checkpoint(json.loads(str(row[1])), blocks)
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
    connection = connect_database(database_path)
    return connection
