"""Crash-safe OKF v0.2 projection of the current SQLite knowledge snapshot."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from openkb.desktop_knowledge_metadata import decode_knowledge_labels
from openkb.desktop_okf_compatibility import lint_okf_projection
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import atomic_write_text, kb_ingest_lock

_OKF_VERSION = "0.2"
logger = logging.getLogger(__name__)
_ENTITY_SUBTYPES = frozenset(
    {
        "API",
        "Dataset",
        "Event",
        "Location",
        "Metric",
        "Organization",
        "Person",
        "Policy",
        "Process",
        "Product",
        "Service",
        "Table",
    }
)


@dataclass(frozen=True)
class _ProjectionSource:
    source_id: str
    evidence_id: str
    document_id: str
    document_name: str
    section: str
    locator: dict[str, object]
    asset_sha256: str
    availability: str


@dataclass(frozen=True)
class _ProjectionDocument:
    identity: str
    kind: str
    title: str
    content_markdown: str
    status: str
    published_at: str
    provenance: str
    authority: Literal["user_revision", "published_generation"]
    revision_number: int | None = None
    generation_id: int | None = None
    source_document_id: str | None = None
    stale_after: str | None = None
    verified_by: str | None = None
    verified_at: str | None = None
    entity_subtype: str | None = None
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    analysis: dict[str, str] | None = None
    sources: tuple[_ProjectionSource, ...] = ()

    @property
    def relative_path(self) -> Path:
        prefix = Path() if self.authority == "user_revision" else Path("generated")
        return prefix / self.kind / f"{self.identity}.md"


def materialize_okf_projection(kb_dir: Path) -> None:
    """Rebuild the complete disposable bundle from the committed SQLite snapshot."""
    resolved = kb_dir.expanduser().resolve()
    with kb_ingest_lock(desktop_state_dir(resolved)):
        _recover_projection_activation(resolved)
        connection = sqlite3.connect(desktop_state_database_path(resolved))
        try:
            staged = stage_okf_projection_in(connection, resolved)
        finally:
            connection.close()
        try:
            activate_okf_projection(resolved, staged)
        finally:
            discard_okf_projection_staging(staged)


def has_valid_okf_projection(kb_dir: Path) -> bool:
    """Check an existing projection without transforming or regenerating it."""
    resolved = kb_dir.expanduser().resolve()
    target = resolved / "knowledge-pages"
    if not target.is_dir():
        return False
    try:
        if lint_okf_projection(target):
            return False
        with sqlite3.connect(desktop_state_database_path(resolved)) as connection:
            expected = _expected_projection_files_in(connection)
        return all((target / relative).is_file() for relative in expected)
    except (OSError, sqlite3.Error, ValueError, yaml.YAMLError):
        return False


def _expected_projection_files_in(connection: sqlite3.Connection) -> tuple[Path, ...]:
    expected = {
        Path("index.md"),
        Path("concept/index.md"),
        Path("entity/index.md"),
        Path("procedure/index.md"),
        Path("generated/index.md"),
        Path("generated/concept/index.md"),
        Path("generated/entity/index.md"),
        Path("generated/procedure/index.md"),
        Path("log.md"),
    }
    expected.update(
        Path(str(row[1])) / f"{row[0]}.md"
        for row in connection.execute(
            """
            SELECT page_id, kind FROM knowledge_pages
            WHERE current_revision_id IS NOT NULL
                AND lifecycle_state IN ('stable', 'deprecated')
            """
        ).fetchall()
    )
    expected.update(
        Path("generated") / str(row[1]) / f"{row[0]}.md"
        for row in connection.execute(
            """
            SELECT items.item_key, items.kind
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            WHERE state.singleton = 1
            """
        ).fetchall()
    )
    return tuple(sorted(expected))


def stage_okf_projection_in(connection: sqlite3.Connection, kb_dir: Path) -> Path:
    """Render one transactional SQLite view into a hidden complete bundle."""
    staging_root = desktop_state_dir(kb_dir) / "okf-projection-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staged = staging_root / uuid.uuid4().hex
    staged.mkdir()
    try:
        documents = _projection_documents_in(connection)
        _render_bundle_in(connection, staged, documents)
        diagnostics = lint_okf_projection(staged)
        if diagnostics:
            summary = ", ".join(f"{item.path}:{item.code}" for item in diagnostics)
            raise RuntimeError(f"Generated OKF projection is invalid: {summary}")
        return staged
    except BaseException:
        discard_okf_projection_staging(staged)
        raise


def activate_okf_projection(kb_dir: Path, staged: Path) -> None:
    """Atomically swap a fully rendered bundle after the authority commit."""
    target = kb_dir / "knowledge-pages"
    backup_root = desktop_state_dir(kb_dir) / "okf-projection-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / uuid.uuid4().hex
    moved_current = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved_current = True
        os.replace(staged, target)
    except BaseException:
        if moved_current and not target.exists() and backup.exists():
            os.replace(backup, target)
        raise
    finally:
        try:
            if backup.exists() and target.exists():
                shutil.rmtree(backup, ignore_errors=True)
            _remove_empty_backup_root(backup_root)
        finally:
            _start_catalog_rebuilds(kb_dir)


def discard_okf_projection_staging(staged: Path) -> None:
    """Remove a hidden bundle that was not, or is no longer, needed."""
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)


def _start_catalog_rebuilds(kb_dir: Path) -> None:
    try:
        from openkb.desktop_catalog_store import start_catalog_rebuilds

        start_catalog_rebuilds(kb_dir)
    except Exception:
        logger.warning("Could not start Knowledge Catalog rebuilds.", exc_info=True)


def canonical_okf_type(kind: str, entity_subtype: str | None = None) -> str:
    """Map OpenKB kind/subtype to one safe OKF type value."""
    if kind == "concept":
        return "Concept"
    if kind == "procedure":
        return "Procedure"
    if kind != "entity":
        raise ValueError(f"Unsupported OpenKB knowledge kind: {kind}")
    return entity_subtype if entity_subtype in _ENTITY_SUBTYPES else "Entity"


def _projection_documents_in(
    connection: sqlite3.Connection,
) -> tuple[_ProjectionDocument, ...]:
    pages = tuple(_published_pages_in(connection))
    generated = tuple(_current_generation_in(connection))
    return tuple(sorted((*pages, *generated), key=lambda item: item.relative_path.as_posix()))


def _published_pages_in(connection: sqlite3.Connection) -> list[_ProjectionDocument]:
    rows = connection.execute(
        """
        SELECT pages.page_id, pages.kind, revisions.revision_id,
            revisions.revision_number, revisions.title, revisions.content_markdown,
            revisions.created_at, revisions.provenance_state, pages.lifecycle_state,
            pages.stale_after, verifications.actor, verifications.verified_at
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        LEFT JOIN knowledge_page_verifications AS verifications
            ON verifications.revision_id = revisions.revision_id
            AND verifications.invalidated_at IS NULL
        WHERE pages.lifecycle_state IN ('stable', 'deprecated')
        ORDER BY pages.kind, pages.page_id
        """
    ).fetchall()
    documents: list[_ProjectionDocument] = []
    for row in rows:
        revision_id = str(row[2])
        provenance = str(row[7])
        sources = (
            () if provenance == "legacy_unmapped" else _revision_sources_in(connection, revision_id)
        )
        documents.append(
            _ProjectionDocument(
                identity=str(row[0]),
                kind=str(row[1]),
                revision_number=int(row[3]),
                title=str(row[4]),
                content_markdown=str(row[5]),
                published_at=str(row[6]),
                provenance=provenance,
                status=str(row[8]),
                stale_after=str(row[9]) if row[9] is not None else None,
                verified_by=str(row[10]) if row[10] is not None else None,
                verified_at=str(row[11]) if row[11] is not None else None,
                authority="user_revision",
                sources=sources,
            )
        )
    return documents


def _revision_sources_in(
    connection: sqlite3.Connection, revision_id: str
) -> tuple[_ProjectionSource, ...]:
    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT sources.source_id, sources.evidence_id, sources.document_id,
                sources.document_name, sources.section, sources.locator_json,
                documents.asset_sha256, documents.availability,
                ROW_NUMBER() OVER (
                    PARTITION BY sources.source_id
                    ORDER BY (documents.availability = 'available') DESC,
                        documents.created_at, documents.document_id
                ) AS source_rank
            FROM knowledge_page_revision_sources AS sources
            JOIN source_documents AS documents ON documents.document_id = sources.document_id
            WHERE sources.revision_id = ?
        )
        SELECT source_id, evidence_id, document_id, document_name, section,
            locator_json, asset_sha256, availability
        FROM ranked WHERE source_rank = 1 ORDER BY source_id
        """,
        (revision_id,),
    ).fetchall()
    return tuple(
        _ProjectionSource(
            source_id=str(row[0]),
            evidence_id=str(row[1]),
            document_id=str(row[2]),
            document_name=str(row[3]),
            section=str(row[4]),
            locator=_locator(str(row[5])),
            asset_sha256=str(row[6]),
            availability=str(row[7]),
        )
        for row in rows
    )


def _current_generation_in(connection: sqlite3.Connection) -> list[_ProjectionDocument]:
    rows = connection.execute(
        """
        SELECT state.current_generation_id, items.item_key, items.kind, items.title,
            items.content_markdown, items.source_document_id, items.created_at,
            items.provenance_state, items.entity_subtype, items.aliases_json,
            items.tags_json, items.analysis_provenance_json
        FROM knowledge_generation_state AS state
        JOIN knowledge_generation_items AS items
            ON items.generation_id = state.current_generation_id
        WHERE state.singleton = 1
        ORDER BY items.kind, items.item_key
        """
    ).fetchall()
    return [
        _ProjectionDocument(
            generation_id=int(row[0]),
            identity=str(row[1]),
            kind=str(row[2]),
            title=str(row[3]),
            content_markdown=str(row[4]),
            source_document_id=str(row[5]),
            published_at=str(row[6]),
            provenance=str(row[7]),
            status="stable",
            authority="published_generation",
            entity_subtype=str(row[8]) if row[8] is not None else None,
            aliases=decode_knowledge_labels(row[9]),
            tags=decode_knowledge_labels(row[10]),
            analysis=_analysis_metadata(row[11]),
            sources=_generation_sources_in(connection, int(row[0]), str(row[1])),
        )
        for row in rows
    ]


def _generation_sources_in(
    connection: sqlite3.Connection, generation_id: int, item_key: str
) -> tuple[_ProjectionSource, ...]:
    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT sources.source_id, sources.evidence_id, documents.document_id,
                documents.display_name, blocks.heading_path, blocks.locator_json,
                documents.asset_sha256, documents.availability,
                ROW_NUMBER() OVER (
                    PARTITION BY sources.source_id
                    ORDER BY (documents.availability = 'available') DESC,
                        documents.created_at, documents.document_id, occurrences.ordinal
                ) AS occurrence_rank
            FROM knowledge_generation_item_sources AS sources
            JOIN evidence_occurrences AS occurrences
                ON occurrences.evidence_id = sources.evidence_id
            JOIN source_documents AS documents
                ON documents.document_id = occurrences.document_id
            JOIN document_ir_blocks AS blocks ON blocks.block_id = occurrences.block_id
            WHERE sources.generation_id = ? AND sources.item_key = ?
        )
        SELECT source_id, evidence_id, document_id, display_name, heading_path,
            locator_json, asset_sha256, availability
        FROM ranked WHERE occurrence_rank = 1 ORDER BY source_id
        """,
        (generation_id, item_key),
    ).fetchall()
    return tuple(
        _ProjectionSource(
            source_id=str(row[0]),
            evidence_id=str(row[1]),
            document_id=str(row[2]),
            document_name=str(row[3]),
            section=_heading_section(str(row[4])),
            locator=_locator(str(row[5])),
            asset_sha256=str(row[6]),
            availability=str(row[7]),
        )
        for row in rows
    )


def _render_bundle_in(
    connection: sqlite3.Connection,
    staged: Path,
    documents: tuple[_ProjectionDocument, ...],
) -> None:
    for relative in (
        Path("concept"),
        Path("entity"),
        Path("procedure"),
        Path("generated"),
        Path("generated/concept"),
        Path("generated/entity"),
        Path("generated/procedure"),
    ):
        (staged / relative).mkdir(parents=True, exist_ok=True)
    for document in documents:
        atomic_write_text(staged / document.relative_path, _render_document(document))
    _render_indexes(staged, documents)
    atomic_write_text(staged / "log.md", _render_change_log_in(connection))


def _render_document(document: _ProjectionDocument) -> str:
    metadata: dict[str, object] = {
        "type": canonical_okf_type(document.kind, document.entity_subtype),
        "title": document.title,
        "status": document.status,
        "generated": {
            "by": (
                "openkb-user-revision/1"
                if document.authority == "user_revision"
                else f"openkb-knowledge-analysis/{document.analysis['schema_version']}"
                if document.analysis is not None
                else "openkb-knowledge-reconciliation/1"
            ),
            "at": document.published_at,
        },
    }
    if document.stale_after is not None:
        metadata["stale_after"] = document.stale_after
    if document.tags:
        metadata["tags"] = list(document.tags)
    if document.verified_by is not None and document.verified_at is not None:
        metadata["verified"] = [
            {"by": _okf_human_actor(document.verified_by), "at": document.verified_at}
        ]
    if document.sources:
        metadata["sources"] = [_source_metadata(source) for source in document.sources]
    extension: dict[str, object] = {
        "kind": document.kind.title(),
        "authority": document.authority,
        "provenance": document.provenance,
    }
    if document.authority == "user_revision":
        extension.update({"page_id": document.identity, "revision": document.revision_number})
    else:
        extension.update(
            {
                "item_key": document.identity,
                "generation": document.generation_id,
                "origin_document_id": document.source_document_id,
            }
        )
        if document.analysis is not None:
            extension["analysis"] = document.analysis
        if document.aliases:
            extension["aliases"] = list(document.aliases)
    metadata["openkb"] = extension
    body = document.content_markdown.rstrip("\n")
    footnotes = "\n".join(
        f"[^{source.source_id}]: {_source_title(source)}" for source in document.sources
    )
    suffix = f"\n\n{footnotes}" if footnotes else ""
    return f"{_yaml_frontmatter(metadata)}\n# {document.title}\n\n{body}{suffix}\n"


def _analysis_metadata(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    fields = ("schema_version", "provider", "model", "prompt_digest", "engine_version")
    if not isinstance(payload, dict) or not all(
        isinstance(payload.get(field), str) and payload[field] for field in fields
    ):
        return None
    return {field: str(payload[field]) for field in fields}


def _source_metadata(source: _ProjectionSource) -> dict[str, object]:
    return {
        "id": source.source_id,
        "resource": f"urn:sha256:{source.asset_sha256}",
        "title": _source_title(source),
        "openkb": {
            "canonical_evidence_id": source.evidence_id,
            "document_id": source.document_id,
            "locator": source.locator,
            "availability": source.availability,
        },
    }


def _source_title(source: _ProjectionSource) -> str:
    return " / ".join(value for value in (source.document_name, source.section) if value)


def _render_indexes(staged: Path, documents: tuple[_ProjectionDocument, ...]) -> None:
    root = (
        f"{_yaml_frontmatter({'okf_version': _OKF_VERSION})}\n"
        "# OpenKB Knowledge\n\n"
        "- [Concepts](concept/index.md)\n"
        "- [Entities](entity/index.md)\n"
        "- [Procedures](procedure/index.md)\n"
        "- [Generated knowledge](generated/index.md)\n"
        "- [Knowledge Change Log](log.md)\n"
    )
    atomic_write_text(staged / "index.md", root)
    _write_listing(staged / "concept/index.md", "Concepts", _documents_at(documents, "concept"))
    _write_listing(staged / "entity/index.md", "Entities", _documents_at(documents, "entity"))
    _write_listing(
        staged / "procedure/index.md", "Procedures", _documents_at(documents, "procedure")
    )
    atomic_write_text(
        staged / "generated/index.md",
        "# Generated knowledge\n\n"
        "- [Concepts](concept/index.md)\n"
        "- [Entities](entity/index.md)\n"
        "- [Procedures](procedure/index.md)\n",
    )
    _write_listing(
        staged / "generated/concept/index.md",
        "Generated Concepts",
        _documents_at(documents, "generated/concept"),
    )
    _write_listing(
        staged / "generated/entity/index.md",
        "Generated Entities",
        _documents_at(documents, "generated/entity"),
    )
    _write_listing(
        staged / "generated/procedure/index.md",
        "Generated Procedures",
        _documents_at(documents, "generated/procedure"),
    )


def _documents_at(
    documents: tuple[_ProjectionDocument, ...], directory: str
) -> tuple[_ProjectionDocument, ...]:
    return tuple(
        sorted(
            (item for item in documents if item.relative_path.parent.as_posix() == directory),
            key=lambda item: (item.title.casefold(), item.identity),
        )
    )


def _write_listing(path: Path, heading: str, documents: tuple[_ProjectionDocument, ...]) -> None:
    lines = [f"# {heading}", ""]
    lines.extend(
        f"- [{_link_label(document.title)}]({document.identity}.md)"
        + (" — deprecated" if document.status == "deprecated" else "")
        for document in documents
    )
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")


def _render_change_log_in(connection: sqlite3.Connection) -> str:
    events: list[tuple[str, str, str, str]] = []
    events.extend(
        (str(row[4]), "revision", str(row[0]), _revision_event(row))
        for row in connection.execute(
            """
            SELECT revisions.revision_id, revisions.page_id, revisions.revision_number,
                revisions.title, revisions.created_at, pages.kind
            FROM knowledge_page_revisions AS revisions
            JOIN knowledge_pages AS pages ON pages.page_id = revisions.page_id
            ORDER BY revisions.created_at DESC, revisions.revision_id
            """
        ).fetchall()
    )
    events.extend(
        (str(row[1]), "generation", str(row[0]), f"Published generation `{row[0]}`.")
        for row in connection.execute(
            "SELECT generation_id, created_at FROM knowledge_generations"
        ).fetchall()
    )
    events.extend(
        (str(row[3]), "lifecycle", str(row[0]), _lifecycle_event(row))
        for row in connection.execute(
            """
            SELECT event_id, page_id, event_type, occurred_at
            FROM knowledge_page_lifecycle_events
            """
        ).fetchall()
    )
    events.extend(
        (str(row[4]), "resolution", str(row[0]), _resolution_event(row))
        for row in connection.execute(
            """
            SELECT resolution_id, candidate_id, decision, target_page_id, resolved_at
            FROM knowledge_reconciliation_resolution_records
            """
        ).fetchall()
    )
    ordered = sorted(events, key=lambda item: (item[0], item[1], item[2]), reverse=True)
    lines = ["# Knowledge Change Log"]
    current_timestamp: str | None = None
    for timestamp, _event_type, _identity, description in ordered:
        if timestamp != current_timestamp:
            lines.extend(("", f"## {timestamp}"))
            current_timestamp = timestamp
        lines.append(f"- {description}")
    return "\n".join(lines).rstrip() + "\n"


def _revision_event(row: tuple[object, ...]) -> str:
    return (
        f"Published revision `{row[2]}` of [{_link_label(str(row[3]))}]"
        f"({row[5]}/{row[1]}.md) for page `{row[1]}`."
    )


def _lifecycle_event(row: tuple[object, ...]) -> str:
    return f"Knowledge page `{row[1]}` lifecycle changed: `{row[2]}`."


def _resolution_event(row: tuple[object, ...]) -> str:
    target = f" for page `{row[3]}`" if row[3] is not None else ""
    return f"Resolved candidate `{row[1]}` with `{row[2]}`{target}."


def _yaml_frontmatter(metadata: dict[str, object]) -> str:
    rendered = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip()
    return f"---\n{rendered}\n---\n"


def _okf_human_actor(actor: str) -> str:
    return actor if actor.startswith("human:") else f"human:{actor.replace('_', '-')}"


def _locator(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _heading_section(value: str) -> str:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, list):
        return ""
    return " / ".join(str(item) for item in parsed if isinstance(item, str))


def _link_label(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]")


def _recover_projection_activation(kb_dir: Path) -> None:
    target = kb_dir / "knowledge-pages"
    backup_root = desktop_state_dir(kb_dir) / "okf-projection-backups"
    if backup_root.exists():
        try:
            backups = tuple(sorted(path for path in backup_root.iterdir() if path.is_dir()))
        except OSError:
            if not target.exists():
                raise
            logger.warning("Could not inspect stale OKF projection backups.", exc_info=True)
            backups = ()
        if not target.exists() and backups:
            os.replace(backups[-1], target)
        for backup in backups:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
        _remove_empty_backup_root(backup_root)
    staging_root = desktop_state_dir(kb_dir) / "okf-projection-staging"
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)


def _remove_empty_backup_root(backup_root: Path) -> None:
    """Treat post-swap backup-directory cleanup as recoverable housekeeping."""
    try:
        if backup_root.exists() and not any(backup_root.iterdir()):
            backup_root.rmdir()
    except OSError:
        logger.warning("Could not remove an empty OKF projection backup directory.", exc_info=True)
