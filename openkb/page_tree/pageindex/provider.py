"""Explicit-evaluation lifecycle for the experimental official PageIndex provider."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from openkb.evaluation.retrieval_types import DesktopPageTreeGenerationIdentity
from openkb.importing.artifacts import DocumentIRBlock, SourceImage
from openkb.locks import atomic_write_text, kb_ingest_lock
from openkb.page_tree.pageindex.adapter import (
    PAGEINDEX_DEFAULT_TIMEOUT_SECONDS,
    PAGEINDEX_PROVIDER_KIND,
    PAGEINDEX_PROVIDER_VERSION,
    PageIndexProviderError,
    ProviderInvoker,
    build_official_pageindex_generation,
    validate_official_pageindex_generation,
)
from openkb.page_tree.store import (
    cleanup_page_tree_generations,
    load_page_tree_generation_in,
    published_page_tree_input_in,
    store_page_tree_generation_in,
)
from openkb.page_tree.tree import (
    PageTreeGeneration,
    PageTreeStageOutcome,
    build_deterministic_page_tree,
    page_tree_checkpoint,
    page_tree_outcome_from_checkpoint,
)
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

logger = logging.getLogger(__name__)
PAGEINDEX_CACHE_SCHEMA = "openkb.official-pageindex-cache.v1"


@dataclass(frozen=True)
class _PageIndexProviderInput:
    blocks: tuple[DocumentIRBlock, ...]
    evidence: tuple[tuple[str, DocumentIRBlock], ...]
    images: tuple[SourceImage, ...]
    shell: PageTreeGeneration


@dataclass(frozen=True)
class PageIndexEvaluationProvider:
    """Immutable run-local view of persisted experimental provider generations."""

    kb_dir: Path
    generations: tuple[DesktopPageTreeGenerationIdentity, ...]
    trees: Mapping[str, PageTreeGeneration]
    degradations: tuple[str, ...] = ()

    @contextmanager
    def lease(self, kb_dir: Path, document_id: str) -> Iterator[PageTreeGeneration | None]:
        if kb_dir.expanduser().resolve() != self.kb_dir:
            yield None
            return
        yield self.trees.get(document_id)


def materialize_official_pageindex_provider(
    kb_dir: Path,
    *,
    python_executable: Path | None = None,
    worker_executable: Path | None = None,
    timeout_seconds: float = PAGEINDEX_DEFAULT_TIMEOUT_SECONDS,
    force_rebuild: bool = False,
    invoke: ProviderInvoker | None = None,
) -> PageIndexEvaluationProvider:
    """Build every Available document from SQLite authority for one fixed evaluation."""
    resolved = kb_dir.expanduser().resolve()
    state_dir = desktop_state_dir(resolved)
    database_path = desktop_state_database_path(resolved)
    cache_dir = (
        state_dir / "provider-cache" / (f"pageindex-{PAGEINDEX_PROVIDER_VERSION.replace('+', '-')}")
    )
    with kb_ingest_lock(state_dir):
        connection = _connect(database_path)
        try:
            document_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT document_id FROM source_documents "
                    "WHERE availability = 'available' ORDER BY document_id"
                ).fetchall()
            )
        finally:
            connection.close()

    identities: list[DesktopPageTreeGenerationIdentity] = []
    trees: dict[str, PageTreeGeneration] = {}
    degradations: list[str] = []
    for document_id in document_ids:
        existing: PageTreeGeneration | None = None
        generation: PageTreeGeneration | None = None
        previous_generation_id: str | None = None
        try:
            with kb_ingest_lock(state_dir):
                connection = _connect(database_path)
                try:
                    provider_input = _provider_input_in(connection, document_id)
                    existing = _load_matching_provider_in(
                        connection, document_id, provider_input.shell
                    )
                    cached = (
                        _cached_generation(
                            _cache_file(cache_dir, provider_input.shell), provider_input.shell
                        )
                        if existing is None and not force_rebuild
                        else None
                    )
                    if cached is not None:
                        generation, previous_generation_id = _publish_provider_generation_in(
                            connection, cached
                        )
                finally:
                    connection.close()
            if existing is not None and not force_rebuild:
                generation = existing
            elif generation is None:
                built = build_official_pageindex_generation(
                    document_id,
                    provider_input.blocks,
                    provider_input.evidence,
                    provider_input.images,
                    python_executable=python_executable,
                    worker_executable=worker_executable,
                    timeout_seconds=timeout_seconds,
                    invoke=invoke,
                )
                with kb_ingest_lock(state_dir):
                    connection = _connect(database_path)
                    try:
                        try:
                            current_input = _provider_input_in(connection, document_id)
                            validate_official_pageindex_generation(built, current_input.shell)
                        except ValueError as error:
                            raise PageIndexProviderError(
                                "pageindex_provider_result_stale",
                                "Official PageIndex input changed while its worker was running.",
                            ) from error
                        _publish_cache(_cache_file(cache_dir, current_input.shell), built)
                        generation, previous_generation_id = _publish_provider_generation_in(
                            connection, built
                        )
                    finally:
                        connection.close()
        except PageIndexProviderError as error:
            logger.warning(
                "Official PageIndex provider degraded document_id=%s code=%s",
                document_id,
                error.code,
            )
            degradations.append(error.code)
            generation = None if error.code == "pageindex_provider_result_stale" else existing
        except (OSError, sqlite3.Error, ValueError):
            logger.warning(
                "Official PageIndex provider could not read one Document IR.", exc_info=True
            )
            degradations.append("pageindex_provider_input_invalid")
            generation = None
        if (
            generation is not None
            and previous_generation_id is not None
            and previous_generation_id != generation.generation_id
        ):
            try:
                cleanup_page_tree_generations(resolved, generation.document_version_id)
            except (OSError, sqlite3.Error, ValueError):
                logger.warning(
                    "Official PageIndex could not clean a superseded generation.", exc_info=True
                )
        if generation is not None:
            trees[document_id] = generation
        identities.append(
            DesktopPageTreeGenerationIdentity(
                document_id=document_id,
                base_generation_id=(generation.generation_id if generation is not None else None),
                provider_kind=PAGEINDEX_PROVIDER_KIND,
                provider_version=PAGEINDEX_PROVIDER_VERSION,
                enrichment_generation_id=None,
            )
        )
    return PageIndexEvaluationProvider(
        resolved,
        tuple(identities),
        MappingProxyType(trees),
        tuple(dict.fromkeys(degradations)),
    )


def detach_official_pageindex_provider_current_for_acceptance(
    kb_dir: Path, document_id: str
) -> None:
    """Detach one disposable acceptance pointer through the provider owner."""
    resolved = kb_dir.expanduser().resolve()
    state_dir = desktop_state_dir(resolved)
    with kb_ingest_lock(state_dir):
        connection = _connect(desktop_state_database_path(resolved))
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM document_page_tree_provider_current
                WHERE document_id = ? AND provider_kind = ? AND provider_version = ?
                """,
                (document_id, PAGEINDEX_PROVIDER_KIND, PAGEINDEX_PROVIDER_VERSION),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _provider_input_in(connection: sqlite3.Connection, document_id: str) -> _PageIndexProviderInput:
    availability = connection.execute(
        "SELECT availability FROM source_documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    if availability != ("available",):
        raise ValueError("Official PageIndex requires the current Available document version.")
    blocks, evidence, images = published_page_tree_input_in(connection, document_id)
    shell = build_deterministic_page_tree(
        document_id,
        blocks,
        evidence,
        images,
        provider_kind=PAGEINDEX_PROVIDER_KIND,
        provider_version=PAGEINDEX_PROVIDER_VERSION,
    )
    return _PageIndexProviderInput(blocks, evidence, images, shell)


def _publish_provider_generation_in(
    connection: sqlite3.Connection, generation: PageTreeGeneration
) -> tuple[PageTreeGeneration, str | None]:
    previous_generation_id: str | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        store_page_tree_generation_in(connection, generation.document_version_id, generation)
        previous = connection.execute(
            """
            SELECT generation_id FROM document_page_tree_provider_current
            WHERE document_id = ? AND provider_kind = ? AND provider_version = ?
            """,
            (
                generation.document_version_id,
                generation.provider_kind,
                generation.provider_version,
            ),
        ).fetchone()
        previous_generation_id = str(previous[0]) if previous is not None else None
        connection.execute(
            """
            INSERT INTO document_page_tree_provider_current (
                document_id, provider_kind, provider_version, generation_id, activated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(document_id, provider_kind, provider_version) DO UPDATE SET
                generation_id = excluded.generation_id,
                activated_at = excluded.activated_at
            """,
            (
                generation.document_version_id,
                generation.provider_kind,
                generation.provider_version,
                generation.generation_id,
                _timestamp(),
            ),
        )
        connection.execute(
            "UPDATE document_page_tree_generations SET status = 'current' WHERE generation_id = ?",
            (generation.generation_id,),
        )
        committed_generation = load_page_tree_generation_in(
            connection, generation.document_version_id, generation.generation_id
        )
        if (
            previous_generation_id is not None
            and previous_generation_id != generation.generation_id
        ):
            _supersede_unreferenced_in(connection, previous_generation_id)
        connection.commit()
        return committed_generation, previous_generation_id
    except BaseException:
        connection.rollback()
        raise


def _cache_file(cache_dir: Path, shell: PageTreeGeneration) -> Path:
    return cache_dir / f"{shell.generation_id}.json"


def _cached_generation(cache_file: Path, expected: PageTreeGeneration) -> PageTreeGeneration | None:
    if not cache_file.is_file():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != PAGEINDEX_CACHE_SCHEMA:
            raise ValueError("Official PageIndex cache schema changed.")
        checkpoint = payload.get("checkpoint")
        if not isinstance(checkpoint, dict) or payload.get("checkpoint_sha256") != _digest(
            checkpoint
        ):
            raise ValueError("Official PageIndex cache integrity check failed.")
        generation = page_tree_outcome_from_checkpoint(checkpoint).generation
        if generation is None:
            raise ValueError("Official PageIndex cache has no generation.")
        validate_official_pageindex_generation(generation, expected)
        return generation
    except (OSError, json.JSONDecodeError, RecursionError, ValueError):
        logger.warning("Ignoring a corrupt official PageIndex provider cache.", exc_info=True)
        return None


def _publish_cache(cache_file: Path, generation: PageTreeGeneration) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = page_tree_checkpoint(
        PageTreeStageOutcome(generation.document_version_id, generation)
    )
    atomic_write_text(
        cache_file,
        json.dumps(
            {
                "schema_version": PAGEINDEX_CACHE_SCHEMA,
                "checkpoint_sha256": _digest(checkpoint),
                "checkpoint": checkpoint,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_matching_provider_in(
    connection: sqlite3.Connection,
    document_id: str,
    expected: PageTreeGeneration,
) -> PageTreeGeneration | None:
    row = connection.execute(
        """
        SELECT current.generation_id
        FROM document_page_tree_provider_current AS current
        JOIN document_page_tree_generations AS generations
            ON generations.generation_id = current.generation_id
        WHERE current.document_id = ? AND current.provider_kind = ?
            AND current.provider_version = ?
            AND generations.structural_ir_fingerprint = ?
            AND generations.locator_mapping_digest = ?
            AND generations.status = 'current'
        """,
        (
            document_id,
            PAGEINDEX_PROVIDER_KIND,
            PAGEINDEX_PROVIDER_VERSION,
            expected.structural_ir_fingerprint,
            expected.locator_mapping_digest,
        ),
    ).fetchone()
    if row is None:
        return None
    return load_page_tree_generation_in(connection, document_id, str(row[0]))


def _supersede_unreferenced_in(connection: sqlite3.Connection, generation_id: str) -> None:
    connection.execute(
        """
        UPDATE document_page_tree_generations SET status = 'superseded'
        WHERE generation_id = ?
            AND NOT EXISTS (
                SELECT 1 FROM document_page_tree_current WHERE generation_id = ?
            )
            AND NOT EXISTS (
                SELECT 1 FROM document_page_tree_provider_current WHERE generation_id = ?
            )
        """,
        (generation_id, generation_id, generation_id),
    )


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = connect_database(database_path)
    return connection
