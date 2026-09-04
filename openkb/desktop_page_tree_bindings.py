"""Read the Evidence and Source Image bindings owned by a PageTree generation."""

from __future__ import annotations

import sqlite3

from openkb.desktop_page_tree import PageTreeEvidenceBinding, PageTreeImageBinding


def load_page_tree_bindings_in(
    connection: sqlite3.Connection,
    generation_id: str,
) -> tuple[
    dict[str, list[PageTreeEvidenceBinding]],
    dict[str, list[PageTreeImageBinding]],
]:
    """Load both binding projections through one generation-scoped interface."""
    evidence_rows = connection.execute(
        """
        SELECT node_id, evidence_id, block_ordinal
        FROM document_page_tree_node_evidence
        WHERE generation_id = ? ORDER BY node_id, association_order
        """,
        (generation_id,),
    ).fetchall()
    evidence: dict[str, list[PageTreeEvidenceBinding]] = {}
    for node_id, evidence_id, ordinal in evidence_rows:
        evidence.setdefault(str(node_id), []).append(
            PageTreeEvidenceBinding(str(evidence_id), int(ordinal))
        )

    image_rows = connection.execute(
        """
        SELECT node_id, source_image_id, image_ordinal
        FROM document_page_tree_node_images
        WHERE generation_id = ? ORDER BY node_id, association_order
        """,
        (generation_id,),
    ).fetchall()
    images: dict[str, list[PageTreeImageBinding]] = {}
    for node_id, image_id, ordinal in image_rows:
        images.setdefault(str(node_id), []).append(
            PageTreeImageBinding(str(image_id), int(ordinal))
        )
    return evidence, images
