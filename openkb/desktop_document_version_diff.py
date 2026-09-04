"""Deterministic all-block Document Version diff construction and persistence."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

VERSION_DIFF_ALGORITHM_VERSION = "openkb.document-version-diff.v1"
ContentChangeKind = Literal["unchanged", "modified", "added", "removed"]
LocationChangeKind = Literal["same", "moved", "unknown"]
_MODIFIED_THRESHOLD = 0.58
_AMBIGUITY_MARGIN = 0.06
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class VersionDiffBlock:
    block_id: str
    evidence_id: str
    ordinal: int
    kind: str
    text: str
    heading_path: tuple[str, ...]
    locator: dict[str, object] | None = None
    media_digest: str | None = None


@dataclass(frozen=True)
class VersionDiffItem:
    old_block_id: str | None
    new_block_id: str | None
    old_evidence_id: str | None
    new_evidence_id: str | None
    content_change_kind: ContentChangeKind
    location_change_kind: LocationChangeKind
    similarity_score: float
    reason_json: str
    old_locator: dict[str, object] | None = None
    new_locator: dict[str, object] | None = None


@dataclass(frozen=True)
class DocumentVersionDiff:
    diff_id: str
    lineage_id: str
    from_document_id: str
    to_document_id: str
    algorithm_version: str
    status: str
    stats: dict[str, int]
    items: tuple[VersionDiffItem, ...]


def match_version_blocks(
    old_blocks: tuple[VersionDiffBlock, ...],
    new_blocks: tuple[VersionDiffBlock, ...],
) -> tuple[VersionDiffItem, ...]:
    """Cover every block once with deterministic matching and conservative ambiguity."""
    old = tuple(sorted(old_blocks, key=lambda block: (block.ordinal, block.block_id)))
    new = tuple(sorted(new_blocks, key=lambda block: (block.ordinal, block.block_id)))
    _validate_blocks(old)
    _validate_blocks(new)
    old_groups = _fingerprint_groups(old)
    new_groups = _fingerprint_groups(new)
    pairs: list[tuple[VersionDiffBlock, VersionDiffBlock, str, float]] = []
    paired_old: set[str] = set()
    paired_new: set[str] = set()
    ambiguous_old: set[str] = set()
    ambiguous_new: set[str] = set()
    for fingerprint in sorted(set(old_groups) & set(new_groups)):
        old_group = old_groups[fingerprint]
        new_group = new_groups[fingerprint]
        if len(old_group) == len(new_group):
            for left, right in zip(old_group, new_group, strict=True):
                pairs.append((left, right, "strict_body", 1.0))
                paired_old.add(left.block_id)
                paired_new.add(right.block_id)
        elif len(old_group) > 1 or len(new_group) > 1:
            ambiguous_old.update(block.block_id for block in old_group)
            ambiguous_new.update(block.block_id for block in new_group)
    remaining_old = tuple(
        block
        for block in old
        if block.block_id not in paired_old and block.block_id not in ambiguous_old
    )
    remaining_new = tuple(
        block
        for block in new
        if block.block_id not in paired_new and block.block_id not in ambiguous_new
    )
    for left, right, score in _modified_pairs(remaining_old, remaining_new):
        pairs.append((left, right, "bounded_similarity", score))
        paired_old.add(left.block_id)
        paired_new.add(right.block_id)
    items = [_paired_item(left, right, reason, score) for left, right, reason, score in pairs]
    items.extend(
        _unpaired_item(block, old_side=True) for block in old if block.block_id not in paired_old
    )
    items.extend(
        _unpaired_item(block, old_side=False) for block in new if block.block_id not in paired_new
    )
    old_ordinals = {block.block_id: block.ordinal for block in old}
    new_ordinals = {block.block_id: block.ordinal for block in new}
    return tuple(
        sorted(
            items,
            key=lambda item: (
                old_ordinals.get(
                    item.old_block_id or "",
                    10**12 + new_ordinals.get(item.new_block_id or "", 0),
                ),
                new_ordinals.get(item.new_block_id or "", 0),
                item.old_block_id or "",
                item.new_block_id or "",
            ),
        )
    )


class DocumentVersionDiffBuilder:
    """Build and persist derived diffs without changing lineage authority."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._state_dir = desktop_state_dir(self._kb_dir)

    def build(
        self,
        *,
        lineage_id: str,
        from_document_id: str,
        to_document_id: str,
        now: str,
    ) -> DocumentVersionDiff:
        with kb_ingest_lock(self._state_dir):
            connection = sqlite3.connect(self._database_path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = build_document_version_diff_in(
                    connection,
                    lineage_id=lineage_id,
                    from_document_id=from_document_id,
                    to_document_id=to_document_id,
                    now=now,
                )
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def list_for_lineage(self, lineage_id: str) -> tuple[DocumentVersionDiff, ...]:
        connection = sqlite3.connect(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT diff_id, from_document_id, to_document_id,
                    algorithm_version, status, stats_json
                FROM document_version_diffs WHERE lineage_id = ?
                """,
                (lineage_id,),
            ).fetchall()
            member_rows = connection.execute(
                """
                SELECT document_id, predecessor_document_id
                FROM document_version_members WHERE source_id = ?
                ORDER BY document_id
                """,
                (lineage_id,),
            ).fetchall()
            positions = _lineage_positions(member_rows)
            rows.sort(
                key=lambda row: (
                    positions.get(str(row[1]), 10**12),
                    positions.get(str(row[2]), 10**12),
                    str(row[0]),
                )
            )
            return tuple(_stored_diff_in(connection, lineage_id, row) for row in rows)
        finally:
            connection.close()

    def record_failed(
        self,
        *,
        lineage_id: str,
        from_document_id: str,
        to_document_id: str,
        now: str,
    ) -> None:
        """Retain a content-free failure marker without undoing confirmed metadata."""
        material = (
            f"{lineage_id}\x1f{from_document_id}\x1f{to_document_id}\x1f"
            f"{VERSION_DIFF_ALGORITHM_VERSION}"
        )
        diff_id = hashlib.sha256(material.encode()).hexdigest()
        with kb_ingest_lock(self._state_dir):
            connection = sqlite3.connect(self._database_path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                with connection:
                    connection.execute(
                        "DELETE FROM document_version_diff_items WHERE diff_id = ?",
                        (diff_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO document_version_diffs (
                            diff_id, lineage_id, from_document_id, to_document_id,
                            algorithm_version, status, stats_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'failed', '{}', ?)
                        ON CONFLICT(diff_id) DO UPDATE SET
                            status = 'failed', stats_json = '{}', created_at = excluded.created_at
                        """,
                        (
                            diff_id,
                            lineage_id,
                            from_document_id,
                            to_document_id,
                            VERSION_DIFF_ALGORITHM_VERSION,
                            now,
                        ),
                    )
            finally:
                connection.close()


def build_document_version_diff_in(
    connection: sqlite3.Connection,
    *,
    lineage_id: str,
    from_document_id: str,
    to_document_id: str,
    now: str,
) -> DocumentVersionDiff:
    _require_confirmed_pair_in(connection, lineage_id, from_document_id, to_document_id)
    old = _blocks_in(connection, from_document_id)
    new = _blocks_in(connection, to_document_id)
    if not old or not new:
        raise DesktopImportError(
            "document_version_diff_unavailable", "Both DocumentIR snapshots must be available."
        )
    items = match_version_blocks(old, new)
    digest_material = (
        f"{lineage_id}\x1f{from_document_id}\x1f{to_document_id}\x1f"
        f"{VERSION_DIFF_ALGORITHM_VERSION}"
    )
    diff_id = hashlib.sha256(digest_material.encode()).hexdigest()
    stats: dict[str, int] = dict(Counter(item.content_change_kind for item in items))
    stats["moved"] = sum(item.location_change_kind == "moved" for item in items)
    connection.execute("DELETE FROM document_version_diffs WHERE diff_id = ?", (diff_id,))
    connection.execute(
        """
        INSERT INTO document_version_diffs (
            diff_id, lineage_id, from_document_id, to_document_id,
            algorithm_version, status, stats_json, created_at
        ) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?)
        """,
        (
            diff_id,
            lineage_id,
            from_document_id,
            to_document_id,
            VERSION_DIFF_ALGORITHM_VERSION,
            json.dumps(stats, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )
    connection.executemany(
        """
        INSERT INTO document_version_diff_items (
            item_id, diff_id, item_order, old_block_id, new_block_id,
            old_evidence_id, new_evidence_id, content_change_kind,
            location_change_kind, similarity_score, reason_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                hashlib.sha256(
                    f"{diff_id}:{order}:{item.old_block_id}:{item.new_block_id}".encode()
                ).hexdigest(),
                diff_id,
                order,
                item.old_block_id,
                item.new_block_id,
                item.old_evidence_id,
                item.new_evidence_id,
                item.content_change_kind,
                item.location_change_kind,
                item.similarity_score,
                item.reason_json,
            )
            for order, item in enumerate(items)
        ),
    )
    return DocumentVersionDiff(
        diff_id,
        lineage_id,
        from_document_id,
        to_document_id,
        VERSION_DIFF_ALGORITHM_VERSION,
        "ready",
        stats,
        items,
    )


def _lineage_positions(rows: list[tuple[object, ...]]) -> dict[str, int]:
    predecessor_by_document = {
        str(document_id): (str(predecessor) if predecessor is not None else None)
        for document_id, predecessor in rows
    }
    children: defaultdict[str | None, list[str]] = defaultdict(list)
    for document_id, predecessor in predecessor_by_document.items():
        parent = predecessor if predecessor in predecessor_by_document else None
        children[parent].append(document_id)
    ordered: list[str] = []
    stack = list(reversed(sorted(children[None])))
    while stack:
        document_id = stack.pop()
        if document_id in ordered:
            continue
        ordered.append(document_id)
        stack.extend(reversed(sorted(children[document_id])))
    ordered.extend(sorted(set(predecessor_by_document) - set(ordered)))
    return {document_id: position for position, document_id in enumerate(ordered)}


def _modified_pairs(
    old: tuple[VersionDiffBlock, ...], new: tuple[VersionDiffBlock, ...]
) -> tuple[tuple[VersionDiffBlock, VersionDiffBlock, float], ...]:
    scores = {
        (left.block_id, right.block_id): _similarity(left, right)
        for left in old
        for right in new
        if left.kind == right.kind
    }
    ranked_old = {
        left.block_id: sorted(
            (
                (scores[(left.block_id, right.block_id)], right)
                for right in new
                if (left.block_id, right.block_id) in scores
            ),
            key=lambda value: (-value[0], value[1].ordinal, value[1].block_id),
        )
        for left in old
    }
    ranked_new = {
        right.block_id: sorted(
            (
                (scores[(left.block_id, right.block_id)], left)
                for left in old
                if (left.block_id, right.block_id) in scores
            ),
            key=lambda value: (-value[0], value[1].ordinal, value[1].block_id),
        )
        for right in new
    }
    candidates = []
    for left in old:
        ranked = ranked_old[left.block_id]
        if not ranked:
            continue
        score, right = ranked[0]
        reverse = ranked_new[right.block_id]
        if (
            score < _MODIFIED_THRESHOLD
            or not reverse
            or reverse[0][1].block_id != left.block_id
            or (len(ranked) > 1 and score - ranked[1][0] < _AMBIGUITY_MARGIN)
            or (len(reverse) > 1 and score - reverse[1][0] < _AMBIGUITY_MARGIN)
        ):
            continue
        candidates.append((left, right, score))
    return tuple(sorted(candidates, key=lambda value: (value[0].ordinal, value[1].ordinal)))


def _similarity(left: VersionDiffBlock, right: VersionDiffBlock) -> float:
    body = difflib.SequenceMatcher(
        None, _normalized_body(left), _normalized_body(right), autojunk=False
    ).ratio()
    path = difflib.SequenceMatcher(
        None,
        "/".join(value.casefold() for value in left.heading_path),
        "/".join(value.casefold() for value in right.heading_path),
        autojunk=False,
    ).ratio()
    media = (
        1.0 if left.media_digest is not None and left.media_digest == right.media_digest else 0.0
    )
    if left.kind == "figure":
        return round((body * 0.35) + (path * 0.15) + (media * 0.5), 6)
    return round((body * 0.82) + (path * 0.18), 6)


def _paired_item(
    left: VersionDiffBlock,
    right: VersionDiffBlock,
    reason: str,
    score: float,
) -> VersionDiffItem:
    unchanged = _strict_fingerprint(left) == _strict_fingerprint(right)
    location: LocationChangeKind = "same" if left.heading_path == right.heading_path else "moved"
    return VersionDiffItem(
        old_block_id=left.block_id,
        new_block_id=right.block_id,
        old_evidence_id=left.evidence_id,
        new_evidence_id=right.evidence_id,
        content_change_kind="unchanged" if unchanged else "modified",
        location_change_kind=location,
        similarity_score=score,
        reason_json=json.dumps(
            {"algorithm": VERSION_DIFF_ALGORITHM_VERSION, "match": reason},
            sort_keys=True,
            separators=(",", ":"),
        ),
        old_locator=left.locator,
        new_locator=right.locator,
    )


def _unpaired_item(block: VersionDiffBlock, *, old_side: bool) -> VersionDiffItem:
    return VersionDiffItem(
        old_block_id=block.block_id if old_side else None,
        new_block_id=None if old_side else block.block_id,
        old_evidence_id=block.evidence_id if old_side else None,
        new_evidence_id=None if old_side else block.evidence_id,
        content_change_kind="removed" if old_side else "added",
        location_change_kind="unknown",
        similarity_score=0.0,
        reason_json=json.dumps(
            {"algorithm": VERSION_DIFF_ALGORITHM_VERSION, "match": "unpaired"},
            sort_keys=True,
            separators=(",", ":"),
        ),
        old_locator=block.locator if old_side else None,
        new_locator=None if old_side else block.locator,
    )


def _strict_fingerprint(block: VersionDiffBlock) -> str:
    payload = {
        "kind": block.kind,
        "body": _normalized_body(block),
        "media_digest": block.media_digest if block.kind == "figure" else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalized_body(block: VersionDiffBlock) -> str:
    value = block.text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if block.kind == "code":
        return "\n".join(line.rstrip() for line in value.splitlines())
    return _SPACE.sub(" ", value).casefold()


def _fingerprint_groups(
    blocks: tuple[VersionDiffBlock, ...],
) -> dict[str, tuple[VersionDiffBlock, ...]]:
    groups: defaultdict[str, list[VersionDiffBlock]] = defaultdict(list)
    for block in blocks:
        groups[_strict_fingerprint(block)].append(block)
    return {key: tuple(value) for key, value in groups.items()}


def _validate_blocks(blocks: tuple[VersionDiffBlock, ...]) -> None:
    ids = [block.block_id for block in blocks]
    if len(ids) != len(set(ids)):
        raise ValueError("Document Version Diff block IDs must be unique per side.")


def _require_confirmed_pair_in(
    connection: sqlite3.Connection,
    lineage_id: str,
    from_document_id: str,
    to_document_id: str,
) -> None:
    rows = connection.execute(
        """
        SELECT members.document_id, members.predecessor_document_id,
            sources.lineage_state, documents.availability
        FROM document_version_members AS members
        JOIN document_version_sources AS sources ON sources.source_id = members.source_id
        JOIN source_documents AS documents ON documents.document_id = members.document_id
        WHERE members.source_id = ? AND members.document_id IN (?, ?)
        """,
        (lineage_id, from_document_id, to_document_id),
    ).fetchall()
    by_id = {str(row[0]): row for row in rows}
    if (
        set(by_id) != {from_document_id, to_document_id}
        or any(str(row[2]) != "confirmed" or str(row[3]) != "available" for row in rows)
        or str(by_id[to_document_id][1]) != from_document_id
    ):
        raise DesktopImportError(
            "document_version_diff_pair_invalid",
            "A persistent diff requires adjacent Available versions in one confirmed lineage.",
        )


def _blocks_in(connection: sqlite3.Connection, document_id: str) -> tuple[VersionDiffBlock, ...]:
    rows = connection.execute(
        """
        SELECT blocks.block_id, occurrences.evidence_id, blocks.ordinal,
            blocks.kind, blocks.text, blocks.heading_path, blocks.locator_json,
            images.image_sha256
        FROM document_ir_blocks AS blocks
        JOIN evidence_occurrences AS occurrences
          ON occurrences.document_id = blocks.document_id
         AND occurrences.block_id = blocks.block_id
        LEFT JOIN source_images AS images
          ON images.document_id = blocks.document_id
         AND images.source_image_id = json_extract(
             blocks.locator_json, '$.source_image_id'
         )
        WHERE blocks.document_id = ? ORDER BY blocks.ordinal
        """,
        (document_id,),
    ).fetchall()
    values = []
    for row in rows:
        try:
            heading_path = json.loads(str(row[5]))
            locator = json.loads(str(row[6]))
        except json.JSONDecodeError as error:
            raise ValueError("DocumentIR block metadata is invalid.") from error
        if (
            not isinstance(heading_path, list)
            or not all(isinstance(value, str) for value in heading_path)
            or not isinstance(locator, dict)
        ):
            raise ValueError("DocumentIR block metadata is invalid.")
        values.append(
            VersionDiffBlock(
                block_id=str(row[0]),
                evidence_id=str(row[1]),
                ordinal=int(row[2]),
                kind=str(row[3]),
                text=str(row[4]),
                heading_path=tuple(heading_path),
                locator=locator,
                media_digest=(
                    str(row[7]) if str(row[3]) == "figure" and row[7] is not None else None
                ),
            )
        )
    return tuple(values)


def _stored_diff_in(
    connection: sqlite3.Connection, lineage_id: str, row: tuple[object, ...]
) -> DocumentVersionDiff:
    diff_id = str(row[0])
    items = tuple(
        _stored_diff_item(item)
        for item in connection.execute(
            """
            SELECT items.old_block_id, items.new_block_id,
                items.old_evidence_id, items.new_evidence_id,
                items.content_change_kind, items.location_change_kind,
                items.similarity_score, items.reason_json,
                old_blocks.locator_json, new_blocks.locator_json
            FROM document_version_diff_items AS items
            LEFT JOIN document_ir_blocks AS old_blocks
              ON old_blocks.block_id = items.old_block_id
            LEFT JOIN document_ir_blocks AS new_blocks
              ON new_blocks.block_id = items.new_block_id
            WHERE items.diff_id = ? ORDER BY items.item_order
            """,
            (diff_id,),
        ).fetchall()
    )
    try:
        stats_value = json.loads(str(row[5]))
    except json.JSONDecodeError as error:
        raise ValueError("Stored Document Version Diff stats are invalid.") from error
    if not isinstance(stats_value, dict) or not all(
        isinstance(key, str) and type(value) is int for key, value in stats_value.items()
    ):
        raise ValueError("Stored Document Version Diff stats are invalid.")
    return DocumentVersionDiff(
        diff_id=diff_id,
        lineage_id=lineage_id,
        from_document_id=str(row[1]),
        to_document_id=str(row[2]),
        algorithm_version=str(row[3]),
        status=str(row[4]),
        stats=stats_value,
        items=items,
    )


def _stored_diff_item(row: tuple[object, ...]) -> VersionDiffItem:
    content_kind = str(row[4])
    location_kind = str(row[5])
    if content_kind not in {"unchanged", "modified", "added", "removed"}:
        raise ValueError("Stored Document Version Diff content kind is invalid.")
    if location_kind not in {"same", "moved", "unknown"}:
        raise ValueError("Stored Document Version Diff location kind is invalid.")
    return VersionDiffItem(
        old_block_id=str(row[0]) if row[0] is not None else None,
        new_block_id=str(row[1]) if row[1] is not None else None,
        old_evidence_id=str(row[2]) if row[2] is not None else None,
        new_evidence_id=str(row[3]) if row[3] is not None else None,
        content_change_kind=cast(ContentChangeKind, content_kind),
        location_change_kind=cast(LocationChangeKind, location_kind),
        similarity_score=_stored_similarity(row[6]),
        reason_json=str(row[7]),
        old_locator=_stored_locator(row[8]),
        new_locator=_stored_locator(row[9]),
    )


def _stored_similarity(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Stored Document Version Diff similarity score is invalid.")
    return float(value)


def _stored_locator(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        locator = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise ValueError("Stored Document Version Diff locator is invalid.") from error
    if not isinstance(locator, dict):
        raise ValueError("Stored Document Version Diff locator is invalid.")
    return locator
