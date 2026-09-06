"""Confirmed Document Lineage catalog with immutable revision snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from typing import Literal, cast

from openkb.documents.version_labels import (
    VersionScheme,
    compare_version_labels,
    parse_version_label,
)
from openkb.importing.artifacts import DesktopImportError

LineageState = Literal["singleton", "confirmed", "needs_order_review"]
SnapshotKind = Literal["full_snapshot", "delta", "unknown"]


@dataclass(frozen=True)
class DocumentVersionMemberDecision:
    document_id: str
    version_label: str
    branch_label: str = "main"
    predecessor_document_id: str | None = None
    snapshot_kind: SnapshotKind = "full_snapshot"
    metadata_origin: str = "user"


@dataclass(frozen=True)
class DocumentLineageDecision:
    display_name: str
    version_scheme: VersionScheme
    members: tuple[DocumentVersionMemberDecision, ...]
    current_document_id: str
    aliases: tuple[str, ...] = ()
    lineage_id: str | None = None
    expected_metadata_revisions: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class DocumentVersionCatalogMember:
    document_id: str
    document_name: str
    availability: str
    version_label: str | None
    normalized_version_label: str | None
    version_key_json: str | None
    branch_label: str | None
    predecessor_document_id: str | None
    snapshot_kind: SnapshotKind
    metadata_origin: str | None
    confirmed_at: str | None


@dataclass(frozen=True)
class DocumentLineage:
    lineage_id: str
    display_name: str
    normalized_name: str
    lineage_state: LineageState
    version_scheme: VersionScheme
    current_document_id: str | None
    metadata_revision: int
    aliases: tuple[str, ...]
    members: tuple[DocumentVersionCatalogMember, ...]


@dataclass(frozen=True)
class DocumentVersionCatalogSnapshot:
    revision_id: str
    source_revision: int
    snapshot_digest: str
    lineages: tuple[DocumentLineage, ...]

    @property
    def document_ids(self) -> frozenset[str]:
        return frozenset(
            member.document_id for lineage in self.lineages for member in lineage.members
        )


def normalize_lineage_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def confirm_document_lineage_in(
    connection: sqlite3.Connection,
    decision: DocumentLineageDecision,
    *,
    now: str,
) -> DocumentVersionCatalogSnapshot:
    """Atomically validate and confirm one complete lineage decision."""
    if not decision.display_name.strip() or not decision.members:
        raise DesktopImportError(
            "invalid_document_lineage", "A lineage needs a name and at least one member."
        )
    member_ids = tuple(member.document_id for member in decision.members)
    if len(member_ids) != len(set(member_ids)):
        raise DesktopImportError(
            "invalid_document_lineage", "A Document Version can appear only once."
        )
    documents = _available_documents_in(connection, member_ids)
    if set(documents) != set(member_ids):
        raise DesktopImportError(
            "document_version_candidate_unavailable",
            "Every confirmed Document Version must remain Available.",
        )
    current = next(
        (
            member
            for member in decision.members
            if member.document_id == decision.current_document_id
        ),
        None,
    )
    if current is None or current.snapshot_kind != "full_snapshot":
        raise DesktopImportError(
            "invalid_document_lineage_current",
            "Current Version must be an Available confirmed full snapshot.",
        )
    parsed = {}
    seen_labels: dict[str, str] = {}
    for member in decision.members:
        label = parse_version_label(member.version_label, decision.version_scheme)
        if label is None:
            raise DesktopImportError(
                "invalid_document_version_label",
                f"{member.version_label!r} does not match {decision.version_scheme}.",
            )
        conflict = seen_labels.get(label.normalized_label)
        if conflict is not None and conflict != member.document_id:
            raise DesktopImportError(
                "document_version_label_conflict",
                "Two different Document Versions cannot share one confirmed label.",
            )
        seen_labels[label.normalized_label] = member.document_id
        parsed[member.document_id] = label
    _validate_predecessors(decision.members, decision.version_scheme)
    old_lineages = _lineages_for_members_in(connection, member_ids)
    _validate_expected_revisions(connection, old_lineages, decision)
    lineage_id = decision.lineage_id or _preferred_lineage_id(connection, member_ids)
    if lineage_id not in old_lineages:
        connection.execute(
            "INSERT INTO document_version_sources (source_id, created_at) VALUES (?, ?)",
            (lineage_id, now),
        )
    next_revision = 1 + max(
        (
            int(row[0])
            for old_id in old_lineages
            if (
                row := connection.execute(
                    "SELECT metadata_revision FROM document_version_sources WHERE source_id = ?",
                    (old_id,),
                ).fetchone()
            )
            is not None
        ),
        default=0,
    )
    connection.execute(
        """
        UPDATE document_version_sources
        SET display_name = ?, normalized_name = ?, lineage_state = 'confirmed',
            version_scheme = ?, current_document_id = ?, current_set_origin = 'user',
            current_set_at = ?, metadata_revision = ?, updated_at = ?
        WHERE source_id = ?
        """,
        (
            decision.display_name.strip(),
            normalize_lineage_name(decision.display_name),
            decision.version_scheme,
            decision.current_document_id,
            now,
            next_revision,
            now,
            lineage_id,
        ),
    )
    for member in decision.members:
        label = parsed[member.document_id]
        connection.execute(
            """
            UPDATE document_version_members
            SET source_id = ?, version_label = ?, normalized_version_label = ?,
                version_key_json = ?, branch_label = ?, predecessor_document_id = ?,
                snapshot_kind = ?, metadata_origin = ?, metadata_confidence = 1.0,
                confirmed_at = ?, linked_at = ?
            WHERE document_id = ?
            """,
            (
                lineage_id,
                member.version_label.strip(),
                label.normalized_label,
                label.key_json,
                member.branch_label.strip() or "main",
                member.predecessor_document_id,
                member.snapshot_kind,
                member.metadata_origin,
                now,
                now,
                member.document_id,
            ),
        )
    aliases = tuple(
        dict.fromkeys((decision.display_name.strip(), *decision.aliases, *documents.values()))
    )
    connection.execute("DELETE FROM document_lineage_aliases WHERE lineage_id = ?", (lineage_id,))
    connection.executemany(
        """
        INSERT INTO document_lineage_aliases (
            lineage_id, alias, normalized_alias, origin, confirmed_at
        ) VALUES (?, ?, ?, 'user', ?)
        """,
        (
            (lineage_id, alias, normalize_lineage_name(alias), now)
            for alias in aliases
            if normalize_lineage_name(alias)
        ),
    )
    connection.execute(
        """
        UPDATE document_version_candidates
        SET status = 'accepted', resolution = 'linked_existing_source', resolved_at = ?
        WHERE status = 'pending' AND document_id IN ({placeholders})
          AND candidate_document_id IN ({placeholders})
        """.format(placeholders=", ".join("?" for _ in member_ids)),
        (now, *member_ids, *member_ids),
    )
    _delete_empty_lineages_in(connection, old_lineages - {lineage_id})
    return publish_document_version_catalog_revision_in(connection, now=now)


def current_document_version_catalog_in(
    connection: sqlite3.Connection,
) -> DocumentVersionCatalogSnapshot | None:
    row = connection.execute(
        """
        SELECT revisions.revision_id, revisions.source_revision,
            revisions.snapshot_digest, revisions.snapshot_json
        FROM document_version_catalog_state AS state
        JOIN document_version_catalog_revisions AS revisions
          ON revisions.revision_id = state.current_revision_id
        WHERE state.singleton = 1
        """
    ).fetchone()
    if row is None:
        return None
    lineages = _decode_lineages(str(row[3]))
    if _snapshot_digest(lineages) != str(row[2]):
        return None
    return DocumentVersionCatalogSnapshot(str(row[0]), int(row[1]), str(row[2]), lineages)


def publish_document_version_catalog_revision_in(
    connection: sqlite3.Connection, *, now: str
) -> DocumentVersionCatalogSnapshot:
    lineages = _live_lineages_in(connection)
    digest = _snapshot_digest(lineages)
    source_row = connection.execute(
        "SELECT source_revision FROM knowledge_catalog_state WHERE singleton = 1"
    ).fetchone()
    source_revision = int(source_row[0]) if source_row is not None else 0
    revision_hash = hashlib.sha256(f"{source_revision}:{digest}".encode()).hexdigest()
    revision_id = f"versions-{revision_hash[:24]}"
    snapshot_json = json.dumps(
        [_lineage_payload(lineage) for lineage in lineages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO document_version_catalog_revisions (
            revision_id, source_revision, snapshot_digest, snapshot_json, created_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_revision, snapshot_digest) DO NOTHING
        """,
        (revision_id, source_revision, digest, snapshot_json, now),
    )
    connection.execute(
        """
        INSERT INTO document_version_catalog_state (singleton, current_revision_id, activated_at)
        VALUES (1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET
            current_revision_id = excluded.current_revision_id,
            activated_at = excluded.activated_at
        """,
        (revision_id, now),
    )
    return DocumentVersionCatalogSnapshot(revision_id, source_revision, digest, lineages)


def backfill_document_version_catalog_in(connection: sqlite3.Connection, *, now: str) -> None:
    """Safely confirm singleton legacy sources and leave multi-member sources unresolved."""
    sources = connection.execute(
        """
        SELECT sources.source_id, COUNT(members.document_id),
            MIN(documents.display_name), MIN(documents.created_at)
        FROM document_version_sources AS sources
        LEFT JOIN document_version_members AS members ON members.source_id = sources.source_id
        LEFT JOIN source_documents AS documents ON documents.document_id = members.document_id
        GROUP BY sources.source_id ORDER BY sources.source_id
        """
    ).fetchall()
    for source_id, count_value, display_name, created_at in sources:
        count = int(count_value)
        current_row = connection.execute(
            "SELECT document_id FROM document_version_members WHERE source_id = ? "
            "ORDER BY document_id LIMIT 1",
            (str(source_id),),
        ).fetchone()
        current_document_id = (
            str(current_row[0]) if count == 1 and current_row is not None else None
        )
        name = str(display_name or source_id)
        connection.execute(
            """
            UPDATE document_version_sources
            SET display_name = ?, normalized_name = ?, lineage_state = ?,
                version_scheme = 'opaque', current_document_id = ?,
                current_set_origin = ?, current_set_at = ?, metadata_revision = 1,
                updated_at = ? WHERE source_id = ?
            """,
            (
                name,
                normalize_lineage_name(name),
                "singleton" if count == 1 else "needs_order_review",
                current_document_id,
                "migration" if count == 1 else None,
                str(created_at or now) if count == 1 else None,
                now,
                str(source_id),
            ),
        )
        connection.execute(
            """
            UPDATE document_version_members
            SET snapshot_kind = ?, metadata_origin = 'migration', confirmed_at = ?
            WHERE source_id = ?
            """,
            (
                "full_snapshot" if count == 1 else "unknown",
                now if count == 1 else None,
                str(source_id),
            ),
        )
        connection.execute(
            """
            INSERT INTO document_lineage_aliases (
                lineage_id, alias, normalized_alias, origin, confirmed_at
            ) VALUES (?, ?, ?, 'migration', ?)
            ON CONFLICT(lineage_id, normalized_alias) DO NOTHING
            """,
            (str(source_id), name, normalize_lineage_name(name), now),
        )
    publish_document_version_catalog_revision_in(connection, now=now)


def _live_lineages_in(connection: sqlite3.Connection) -> tuple[DocumentLineage, ...]:
    rows = connection.execute(
        """
        SELECT source_id, display_name, normalized_name, lineage_state,
            version_scheme, current_document_id, metadata_revision
        FROM document_version_sources ORDER BY normalized_name, source_id
        """
    ).fetchall()
    values = []
    for row in rows:
        lineage_id = str(row[0])
        aliases = tuple(
            str(alias[0])
            for alias in connection.execute(
                "SELECT alias FROM document_lineage_aliases WHERE lineage_id = ? "
                "ORDER BY normalized_alias",
                (lineage_id,),
            ).fetchall()
        )
        unsorted_members = tuple(
            _member_from_row(member)
            for member in connection.execute(
                """
                SELECT members.document_id, documents.display_name, documents.availability,
                    members.version_label, members.normalized_version_label,
                    members.version_key_json, members.branch_label,
                    members.predecessor_document_id, members.snapshot_kind,
                    members.metadata_origin, members.confirmed_at
                FROM document_version_members AS members
                JOIN source_documents AS documents ON documents.document_id = members.document_id
                WHERE members.source_id = ?
                ORDER BY COALESCE(members.branch_label, ''), members.document_id
                """,
                (lineage_id,),
            ).fetchall()
        )
        scheme = str(row[4])
        members = tuple(
            sorted(
                unsorted_members,
                key=lambda member: _member_sort_key(member, scheme),
            )
        )
        values.append(
            DocumentLineage(
                lineage_id=lineage_id,
                display_name=str(row[1]),
                normalized_name=str(row[2]),
                lineage_state=str(row[3]),  # type: ignore[arg-type]
                version_scheme=str(row[4]),  # type: ignore[arg-type]
                current_document_id=str(row[5]) if row[5] is not None else None,
                metadata_revision=int(row[6]),
                aliases=aliases,
                members=members,
            )
        )
    return tuple(values)


def _decode_lineages(value: str) -> tuple[DocumentLineage, ...]:
    try:
        payload = json.loads(value)
        if not isinstance(payload, list):
            raise ValueError
        return tuple(_decode_lineage(item) for item in payload)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Document Version Catalog snapshot is invalid.") from error


def _decode_lineage(value: object) -> DocumentLineage:
    item = _exact_object(
        value,
        {
            "lineage_id",
            "display_name",
            "normalized_name",
            "lineage_state",
            "version_scheme",
            "current_document_id",
            "metadata_revision",
            "aliases",
            "members",
        },
    )
    lineage_state = _enum_string(
        item["lineage_state"], {"singleton", "confirmed", "needs_order_review"}
    )
    version_scheme = _enum_string(
        item["version_scheme"], {"numeric_dotted", "semver", "calendar", "opaque"}
    )
    aliases_value = item["aliases"]
    members_value = item["members"]
    if not isinstance(aliases_value, list) or not all(
        isinstance(alias, str) for alias in aliases_value
    ):
        raise ValueError
    if not isinstance(members_value, list) or not members_value:
        raise ValueError
    return DocumentLineage(
        lineage_id=_required_string(item["lineage_id"]),
        display_name=_required_string(item["display_name"]),
        normalized_name=_required_string(item["normalized_name"]),
        lineage_state=cast(LineageState, lineage_state),
        version_scheme=cast(VersionScheme, version_scheme),
        current_document_id=_optional_string(item["current_document_id"]),
        metadata_revision=_positive_int(item["metadata_revision"]),
        aliases=tuple(aliases_value),
        members=tuple(_decode_catalog_member(member) for member in members_value),
    )


def _decode_catalog_member(value: object) -> DocumentVersionCatalogMember:
    item = _exact_object(
        value,
        {
            "document_id",
            "document_name",
            "availability",
            "version_label",
            "normalized_version_label",
            "version_key_json",
            "branch_label",
            "predecessor_document_id",
            "snapshot_kind",
            "metadata_origin",
            "confirmed_at",
        },
    )
    snapshot_kind = _enum_string(item["snapshot_kind"], {"full_snapshot", "delta", "unknown"})
    return DocumentVersionCatalogMember(
        document_id=_required_string(item["document_id"]),
        document_name=_required_string(item["document_name"]),
        availability=_required_string(item["availability"]),
        version_label=_optional_string(item["version_label"]),
        normalized_version_label=_optional_string(item["normalized_version_label"]),
        version_key_json=_optional_string(item["version_key_json"]),
        branch_label=_optional_string(item["branch_label"]),
        predecessor_document_id=_optional_string(item["predecessor_document_id"]),
        snapshot_kind=cast(SnapshotKind, snapshot_kind),
        metadata_origin=_optional_string(item["metadata_origin"]),
        confirmed_at=_optional_string(item["confirmed_at"]),
    )


def _exact_object(value: object, fields: set[str]) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or any(not isinstance(key, str) for key in value)
    ):
        raise ValueError
    return cast(dict[str, object], value)


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _required_string(value)


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError
    return value


def _enum_string(value: object, allowed: set[str]) -> str:
    candidate = _required_string(value)
    if candidate not in allowed:
        raise ValueError
    return candidate


def _lineage_payload(lineage: DocumentLineage) -> dict[str, object]:
    return {
        **asdict(lineage),
        "members": [asdict(member) for member in lineage.members],
    }


def _snapshot_digest(lineages: tuple[DocumentLineage, ...]) -> str:
    payload = [_lineage_payload(lineage) for lineage in lineages]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _member_from_row(row: tuple[object, ...]) -> DocumentVersionCatalogMember:
    return DocumentVersionCatalogMember(
        document_id=str(row[0]),
        document_name=str(row[1]),
        availability=str(row[2]),
        version_label=str(row[3]) if row[3] is not None else None,
        normalized_version_label=str(row[4]) if row[4] is not None else None,
        version_key_json=str(row[5]) if row[5] is not None else None,
        branch_label=str(row[6]) if row[6] is not None else None,
        predecessor_document_id=str(row[7]) if row[7] is not None else None,
        snapshot_kind=str(row[8]),  # type: ignore[arg-type]
        metadata_origin=str(row[9]) if row[9] is not None else None,
        confirmed_at=str(row[10]) if row[10] is not None else None,
    )


def _member_sort_key(member: DocumentVersionCatalogMember, scheme: str) -> tuple[object, ...]:
    parsed = (
        parse_version_label(member.version_label, scheme)  # type: ignore[arg-type]
        if member.version_label is not None
        else None
    )
    return (
        member.branch_label or "",
        0 if parsed is not None and parsed.order_key is not None else 1,
        parsed.order_key if parsed is not None and parsed.order_key is not None else (),
        member.document_id,
    )


def _available_documents_in(
    connection: sqlite3.Connection, document_ids: tuple[str, ...]
) -> dict[str, str]:
    placeholders = ", ".join("?" for _ in document_ids)
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            f"SELECT document_id, display_name FROM source_documents "
            f"WHERE availability = 'available' AND document_id IN ({placeholders})",
            document_ids,
        ).fetchall()
    }


def _lineages_for_members_in(
    connection: sqlite3.Connection, document_ids: tuple[str, ...]
) -> set[str]:
    placeholders = ", ".join("?" for _ in document_ids)
    return {
        str(row[0])
        for row in connection.execute(
            f"SELECT DISTINCT source_id FROM document_version_members "
            f"WHERE document_id IN ({placeholders})",
            document_ids,
        ).fetchall()
    }


def _preferred_lineage_id(connection: sqlite3.Connection, document_ids: tuple[str, ...]) -> str:
    placeholders = ", ".join("?" for _ in document_ids)
    row = connection.execute(
        f"""
        SELECT members.source_id
        FROM document_version_members AS members
        JOIN source_documents AS documents ON documents.document_id = members.document_id
        WHERE members.document_id IN ({placeholders})
        ORDER BY documents.created_at, members.document_id LIMIT 1
        """,
        document_ids,
    ).fetchone()
    return str(row[0]) if row is not None else uuid.uuid4().hex


def _validate_expected_revisions(
    connection: sqlite3.Connection,
    lineage_ids: set[str],
    decision: DocumentLineageDecision,
) -> None:
    expected = dict(decision.expected_metadata_revisions)
    if not expected:
        return
    actual = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT source_id, metadata_revision FROM document_version_sources"
        ).fetchall()
        if str(row[0]) in lineage_ids
    }
    if expected != actual:
        raise DesktopImportError(
            "document_lineage_revision_conflict",
            "The Document Lineage changed after this review was opened.",
        )


def _validate_predecessors(
    members: tuple[DocumentVersionMemberDecision, ...],
    scheme: VersionScheme,
) -> None:
    by_id = {member.document_id: member for member in members}
    for member in members:
        predecessor_id = member.predecessor_document_id
        if predecessor_id is None:
            continue
        predecessor = by_id.get(predecessor_id)
        if predecessor is None or predecessor.branch_label != member.branch_label:
            raise DesktopImportError(
                "invalid_document_version_predecessor",
                "A predecessor must be another member of the same branch.",
            )
        comparison = compare_version_labels(predecessor.version_label, member.version_label, scheme)
        if scheme != "opaque" and comparison != -1:
            raise DesktopImportError(
                "invalid_document_version_order",
                "A predecessor label must sort before its successor in the confirmed scheme.",
            )
    for start in by_id:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                raise DesktopImportError(
                    "document_version_predecessor_cycle",
                    "Document Version predecessors cannot contain a cycle.",
                )
            seen.add(current)
            current = by_id[current].predecessor_document_id


def _delete_empty_lineages_in(connection: sqlite3.Connection, lineage_ids: set[str]) -> None:
    for lineage_id in lineage_ids:
        connection.execute(
            "DELETE FROM document_version_sources WHERE source_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM document_version_members WHERE source_id = ?)",
            (lineage_id, lineage_id),
        )
