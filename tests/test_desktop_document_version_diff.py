"""Deterministic, complete, and conservative Document Version block matching."""

from __future__ import annotations

import sqlite3

from openkb.desktop_document_version_diff import (
    VersionDiffBlock,
    _blocks_in,
    match_version_blocks,
)


def _block(
    side: str,
    ordinal: int,
    kind: str,
    text: str,
    path: tuple[str, ...],
    *,
    media_digest: str | None = None,
) -> VersionDiffBlock:
    return VersionDiffBlock(
        block_id=f"{side}-{ordinal}",
        evidence_id=f"evidence-{side}-{ordinal}",
        ordinal=ordinal,
        kind=kind,
        text=text,
        heading_path=path,
        media_digest=media_digest,
    )


def test_diff_covers_text_list_table_code_and_figure_blocks() -> None:
    old = (
        _block("old", 0, "heading", "Overview", ("Overview",)),
        _block("old", 1, "paragraph", "Stable introduction.", ("Overview",)),
        _block("old", 2, "list", "- alpha\n- beta", ("Steps",)),
        _block("old", 3, "table", "name | value\na | 1", ("Data",)),
        _block("old", 4, "code", "deploy --mode safe", ("Commands",)),
        _block("old", 5, "figure", "Topology", ("Old section",), media_digest="a" * 64),
        _block("old", 6, "paragraph", "Removed note.", ("Notes",)),
    )
    new = (
        _block("new", 0, "heading", "Overview", ("Overview",)),
        _block("new", 1, "paragraph", "Stable introduction.", ("Overview",)),
        _block("new", 2, "list", "- alpha\n- beta\n- gamma", ("Steps",)),
        _block("new", 3, "table", "name | value\na | 2", ("Data",)),
        _block("new", 4, "code", "deploy --mode fast", ("Commands",)),
        _block("new", 5, "figure", "Topology", ("New section",), media_digest="a" * 64),
        _block("new", 6, "paragraph", "Added note.", ("Notes",)),
    )

    result = match_version_blocks(old, new)

    assert {item.old_block_id for item in result if item.old_block_id} == {
        block.block_id for block in old
    }
    assert {item.new_block_id for item in result if item.new_block_id} == {
        block.block_id for block in new
    }
    assert len(result) == 7
    by_old = {item.old_block_id: item for item in result}
    assert by_old["old-0"].content_change_kind == "unchanged"
    assert by_old["old-2"].content_change_kind == "modified"
    assert by_old["old-3"].content_change_kind == "modified"
    assert by_old["old-4"].content_change_kind == "modified"
    assert by_old["old-5"].content_change_kind == "unchanged"
    assert by_old["old-5"].location_change_kind == "moved"
    assert by_old["old-6"].content_change_kind == "modified"


def test_ambiguous_duplicate_anchor_is_conservatively_removed_and_added() -> None:
    old = (
        _block("old", 1, "paragraph", "Repeated exact block", ("Same",)),
        _block("old", 2, "paragraph", "Repeated exact block", ("Same",)),
    )
    new = (_block("new", 9, "paragraph", "Repeated exact block", ("Same",)),)

    result = match_version_blocks(old, new)

    assert [item.content_change_kind for item in result].count("removed") == 2
    assert [item.content_change_kind for item in result].count("added") == 1


def test_diff_output_is_independent_of_input_tuple_order() -> None:
    old = (
        _block("old", 0, "paragraph", "One stable block", ("A",)),
        _block("old", 1, "paragraph", "A changed block", ("B",)),
    )
    new = (
        _block("new", 0, "paragraph", "One stable block", ("A",)),
        _block("new", 1, "paragraph", "The changed block", ("B",)),
    )

    assert match_version_blocks(old, new) == match_version_blocks(
        tuple(reversed(old)), tuple(reversed(new))
    )


def test_persisted_figure_blocks_use_their_own_source_image_digest() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE document_ir_blocks (
            block_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            heading_path TEXT NOT NULL,
            locator_json TEXT NOT NULL
        );
        CREATE TABLE evidence_occurrences (
            document_id TEXT NOT NULL,
            block_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL
        );
        CREATE TABLE source_images (
            source_image_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            image_sha256 TEXT NOT NULL
        );
        INSERT INTO document_ir_blocks VALUES
            ('figure-1', 'document-1', 0, 'figure', 'First', '["Figures"]',
             '{"source_image_id":"image-1"}'),
            ('figure-2', 'document-1', 1, 'figure', 'Second', '["Figures"]',
             '{"source_image_id":"image-2"}');
        INSERT INTO evidence_occurrences VALUES
            ('document-1', 'figure-1', 'evidence-1'),
            ('document-1', 'figure-2', 'evidence-2');
        INSERT INTO source_images VALUES
            ('image-1', 'document-1', 0,
             'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' || 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'),
            ('image-2', 'document-1', 1,
             'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' || 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');
        """
    )

    blocks = _blocks_in(connection, "document-1")

    assert [block.media_digest for block in blocks] == ["a" * 64, "b" * 64]
