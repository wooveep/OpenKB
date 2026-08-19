"""Explicit-evaluation lifecycle for the experimental official PageIndex provider."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from openkb.desktop_page_tree import PageTreeGeneration, build_deterministic_page_tree
from openkb.desktop_page_tree_store import (
    cleanup_page_tree_generations,
    load_page_tree_generation_in,
    published_page_tree_input_in,
    store_page_tree_generation_in,
)
from openkb.desktop_pageindex_adapter import (
    PAGEINDEX_DEFAULT_TIMEOUT_SECONDS,
    PAGEINDEX_PROVIDER_KIND,
    PAGEINDEX_PROVIDER_VERSION,
    PageIndexProviderError,
    ProviderInvoker,
    build_official_pageindex_generation,
)
from openkb.desktop_retrieval_evaluation_types import DesktopPageTreeGenerationIdentity
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

logger = logging.getLogger(__name__)


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
    python_executable: Path | None,
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
        try:
            with kb_ingest_lock(state_dir):
                connection = _connect(database_path)
                try:
                    blocks, evidence, images = published_page_tree_input_in(connection, document_id)
                    shell = build_deterministic_page_tree(
                        document_id,
                        blocks,
                        evidence,
                        images,
                        provider_kind=PAGEINDEX_PROVIDER_KIND,
                        provider_version=PAGEINDEX_PROVIDER_VERSION,
                    )
                    existing = _load_matching_provider_in(connection, document_id, shell)
                finally:
                    connection.close()
            if existing is not None and not force_rebuild:
                generation = existing
            else:
                generation = build_official_pageindex_generation(
                    document_id,
                    blocks,
                    evidence,
                    images,
                    cache_dir=cache_dir,
                    python_executable=python_executable,
                    timeout_seconds=timeout_seconds,
                    invoke=invoke,
                    allow_cache=not force_rebuild,
                )
                generation = _persist_provider_generation(resolved, generation)
        except PageIndexProviderError as error:
            logger.warning(
                "Official PageIndex provider degraded document_id=%s code=%s",
                document_id,
                error.code,
            )
            degradations.append(error.code)
            generation = existing
        except (OSError, sqlite3.Error, ValueError):
            logger.warning(
                "Official PageIndex provider could not read one Document IR.", exc_info=True
            )
            degradations.append("pageindex_provider_input_invalid")
            generation = None
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


def _persist_provider_generation(
    kb_dir: Path, generation: PageTreeGeneration
) -> PageTreeGeneration:
    state_dir = desktop_state_dir(kb_dir)
    database_path = desktop_state_database_path(kb_dir)
    previous_generation_id: str | None = None
    with kb_ingest_lock(state_dir):
        connection = _connect(database_path)
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
                "UPDATE document_page_tree_generations SET status = 'current' "
                "WHERE generation_id = ?",
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
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    if previous_generation_id is not None and previous_generation_id != generation.generation_id:
        cleanup_page_tree_generations(kb_dir, generation.document_version_id)
    return committed_generation


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
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
