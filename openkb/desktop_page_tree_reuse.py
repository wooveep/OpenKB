"""Strict D1 reuse lineage for immutable Document PageTree generations."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from openkb.desktop_page_tree import PageTreeGeneration


class PageTreeCanonicalDependencyError(RuntimeError):
    """The D1 authority must reach the requested provider before this generation can publish."""

    def __init__(self, canonical_document_id: str) -> None:
        super().__init__("The canonical Document PageTree provider is not ready.")
        self.canonical_document_id = canonical_document_id


def require_d1_canonical_provider_in(
    connection: sqlite3.Connection,
    document_id: str,
    generation: PageTreeGeneration,
) -> None:
    """Defer a D1 generation while its canonical authority targets another provider."""
    row = connection.execute(
        """
        SELECT fingerprints.canonical_document_id, canonical.provider_kind,
            canonical.provider_version
        FROM document_content_fingerprints AS fingerprints
        LEFT JOIN document_page_tree_current AS current
            ON current.document_id = fingerprints.canonical_document_id
        LEFT JOIN document_page_tree_generations AS canonical
            ON canonical.generation_id = current.generation_id
        WHERE fingerprints.document_id = ?
            AND fingerprints.canonical_document_id IS NOT NULL
            AND fingerprints.canonical_document_id != fingerprints.document_id
        """,
        (document_id,),
    ).fetchone()
    if row is not None and (str(row[1]), str(row[2])) != (
        generation.provider_kind,
        generation.provider_version,
    ):
        raise PageTreeCanonicalDependencyError(str(row[0]))


def reuse_matching_d1_generation_in(
    connection: sqlite3.Connection,
    document_id: str,
    generation: PageTreeGeneration,
) -> PageTreeGeneration:
    """Link a version-bound generation only to an exact structure/locator match."""
    row = connection.execute(
        """
        SELECT canonical.generation_id, canonical.provider_kind, canonical.provider_version,
            canonical.structural_ir_fingerprint, canonical.locator_mapping_digest
        FROM document_content_fingerprints AS fingerprints
        JOIN document_page_tree_current AS current
            ON current.document_id = fingerprints.canonical_document_id
        JOIN document_page_tree_generations AS canonical
            ON canonical.generation_id = current.generation_id
        WHERE fingerprints.document_id = ?
            AND fingerprints.canonical_document_id IS NOT NULL
        """,
        (document_id,),
    ).fetchone()
    if row is None or tuple(str(value) for value in row[1:]) != (
        generation.provider_kind,
        generation.provider_version,
        generation.structural_ir_fingerprint,
        generation.locator_mapping_digest,
    ):
        return generation
    return replace(generation, reused_from_generation_id=str(row[0]))
