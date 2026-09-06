"""Atomic, user-selected exports of the current OKF Knowledge Projection."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from openkb.documents.source_image_locator import source_image_matches_evidence
from openkb.knowledge.export.portable_wiki_export import render_portable_wiki_in
from openkb.knowledge.export.portable_wiki_preview import (
    portable_wiki_preview_in,
    portable_wiki_snapshot_in,
)
from openkb.knowledge.export.portable_wiki_validation import portable_wiki_snapshot_id
from openkb.knowledge.pages.okf_projection import (
    discard_okf_projection_staging,
    stage_okf_projection_in,
)
from openkb.locks import atomic_write_text, kb_ingest_lock
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir
from openkb.workspace.runtime import DesktopKnowledgeBaseError

DesktopKnowledgeExportMode = Literal["knowledge_projection", "self_contained", "portable_wiki"]


class DesktopKnowledgeExportError(DesktopKnowledgeBaseError):
    """A stable, actionable failure for a user-requested Knowledge export."""

    def __init__(self, message: str) -> None:
        super().__init__("knowledge_export_failed", message)


@dataclass(frozen=True)
class DesktopKnowledgeExport:
    path: str
    mode: DesktopKnowledgeExportMode
    files: tuple[str, ...]
    raw_asset_count: int
    source_image_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "mode": self.mode,
            "files": list(self.files),
            "raw_asset_count": self.raw_asset_count,
            "source_image_count": self.source_image_count,
        }


@dataclass(frozen=True)
class DesktopKnowledgeExportPreview:
    mode: DesktopKnowledgeExportMode
    document_count: int
    estimated_size_bytes: int
    snapshot_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "document_count": self.document_count,
            "estimated_size_bytes": self.estimated_size_bytes,
            "snapshot_id": self.snapshot_id,
        }


@dataclass(frozen=True)
class _SourceMapping:
    source_id: str
    evidence_id: str
    document_id: str
    locator: dict[str, object]


@dataclass(frozen=True)
class _SourceAsset:
    asset_sha256: str
    raw_path: str
    original_name: str
    media_type: str
    display_name: str
    source_format: str
    availability: str
    mappings: tuple[_SourceMapping, ...]


@dataclass(frozen=True)
class _SourceImage:
    source_image_id: str
    document_id: str
    image_sha256: str
    media_type: str
    storage_path: str
    display_name: str
    alt_text: str | None


class DesktopKnowledgeExportService:
    """Export one immutable snapshot without mutating SQLite identities or lifecycle."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(self._kb_dir)
        self._database_path = desktop_state_database_path(self._kb_dir)

    def preview(self, *, mode: DesktopKnowledgeExportMode) -> DesktopKnowledgeExportPreview:
        """Return the user-visible size/count estimate before Portable Wiki export."""
        if mode != "portable_wiki":
            raise DesktopKnowledgeExportError("Preview is available for Portable Wiki export.")
        if not self._database_path.is_file():
            raise DesktopKnowledgeExportError(
                "Open a Desktop Knowledge Base before previewing its knowledge."
            )
        with kb_ingest_lock(self._state_dir):
            connection = sqlite3.connect(self._database_path)
            try:
                connection.execute("BEGIN")
                preview = portable_wiki_preview_in(connection, self._kb_dir)
            finally:
                connection.rollback()
                connection.close()
        return DesktopKnowledgeExportPreview(
            mode=mode,
            document_count=preview.document_count,
            estimated_size_bytes=preview.estimated_size_bytes,
            snapshot_id=preview.snapshot_id,
        )

    def export(
        self,
        destination_directory: Path,
        *,
        mode: DesktopKnowledgeExportMode,
        expected_snapshot_id: str | None = None,
    ) -> DesktopKnowledgeExport:
        parent = destination_directory.expanduser().resolve()
        if mode not in {"knowledge_projection", "self_contained", "portable_wiki"}:
            raise DesktopKnowledgeExportError("Choose a supported Knowledge Bundle type.")
        if not parent.is_dir():
            raise DesktopKnowledgeExportError("The selected export folder is unavailable.")
        if parent == self._kb_dir or self._kb_dir in parent.parents:
            raise DesktopKnowledgeExportError(
                "Choose an export folder outside the active Knowledge Base."
            )
        if not self._database_path.is_file():
            raise DesktopKnowledgeExportError(
                "Open a Desktop Knowledge Base before exporting its knowledge."
            )
        if mode == "portable_wiki" and not expected_snapshot_id:
            raise DesktopKnowledgeExportError("Preview the Portable Wiki before exporting it.")

        staging: Path | None = None
        final = parent / _export_directory_name(mode)
        projection_staging: Path | None = None
        raw_asset_count = 0
        source_image_count = 0
        try:
            with kb_ingest_lock(self._state_dir):
                _discard_abandoned_export_staging(parent, self._kb_dir)
                staging = Path(
                    tempfile.mkdtemp(prefix=_export_staging_prefix(self._kb_dir), dir=parent)
                )
                connection = sqlite3.connect(self._database_path)
                try:
                    connection.execute("BEGIN")
                    if mode == "portable_wiki":
                        current_snapshot_id = portable_wiki_snapshot_id(
                            portable_wiki_snapshot_in(connection)
                        )
                        if current_snapshot_id != expected_snapshot_id:
                            raise DesktopKnowledgeExportError(
                                "The Portable Wiki preview has changed. Refresh it and retry."
                            )
                        portable = render_portable_wiki_in(connection, self._kb_dir, staging)
                        source_image_count = portable.source_image_count
                    else:
                        projection_snapshot = stage_okf_projection_in(connection, self._kb_dir)
                        projection_staging = projection_snapshot
                        sources = _source_assets_in(connection)
                        mappings = tuple(
                            mapping for source in sources for mapping in source.mappings
                        )
                        images = _source_images_in(connection, mappings)
                        shutil.copytree(projection_snapshot, staging, dirs_exist_ok=True)
                        raw_resources: dict[str, str] = {}
                        image_resources: dict[str, str] = {}
                        if mode == "self_contained":
                            raw_resources = self._copy_raw_assets(staging, sources)
                            image_resources = self._copy_source_images(staging, images)
                            _rewrite_projection_resources(staging, raw_resources)
                        manifest = _manifest(
                            mode=mode,
                            sources=sources,
                            images=images,
                            raw_resources=raw_resources,
                            image_resources=image_resources,
                        )
                        atomic_write_text(
                            staging / "source-manifest.json",
                            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                            + "\n",
                        )
                        raw_asset_count = len(raw_resources)
                        source_image_count = len(image_resources)
                finally:
                    connection.rollback()
                    connection.close()
                    if projection_staging is not None:
                        discard_okf_projection_staging(projection_staging)
                files = tuple(
                    path.relative_to(staging).as_posix()
                    for path in sorted(staging.rglob("*"))
                    if path.is_file()
                )
                os.replace(staging, final)
            return DesktopKnowledgeExport(
                path=str(final),
                mode=mode,
                files=files,
                raw_asset_count=raw_asset_count,
                source_image_count=source_image_count,
            )
        except DesktopKnowledgeExportError:
            raise
        except (OSError, sqlite3.Error, ValueError, yaml.YAMLError) as error:
            raise DesktopKnowledgeExportError(
                "The Knowledge Bundle could not be exported. Choose another folder and retry."
            ) from error
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _copy_raw_assets(self, staging: Path, sources: tuple[_SourceAsset, ...]) -> dict[str, str]:
        resources: dict[str, str] = {}
        for source in sources:
            if source.availability != "available":
                raise DesktopKnowledgeExportError(
                    f'The referenced document "{source.display_name}" is unavailable.'
                )
            source_path = _stored_file(self._kb_dir, source.raw_path)
            if not source_path.is_file():
                raise DesktopKnowledgeExportError(
                    f'The referenced document "{source.display_name}" is missing.'
                )
            relative = (Path("raw") / source_path.name).as_posix()
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
            resources[source.asset_sha256] = relative
        return resources

    def _copy_source_images(
        self, staging: Path, images: tuple[_SourceImage, ...]
    ) -> dict[str, str]:
        resources: dict[str, str] = {}
        for image in images:
            source_path = _stored_file(self._kb_dir, image.storage_path)
            if not source_path.is_file():
                raise DesktopKnowledgeExportError(
                    f'The referenced source image "{image.display_name}" is missing.'
                )
            suffix = source_path.suffix.lower() or ".bin"
            relative = (Path("images") / f"{image.image_sha256}{suffix}").as_posix()
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source_path, target)
            resources[image.source_image_id] = relative
        return resources


def _source_assets_in(connection: sqlite3.Connection) -> tuple[_SourceAsset, ...]:
    rows = connection.execute(
        """
        WITH generated_ranked AS (
            SELECT raw.asset_sha256, raw.raw_path, raw.original_name, raw.media_type,
                documents.display_name, documents.source_format, documents.availability,
                sources.source_id, sources.evidence_id, documents.document_id,
                blocks.locator_json,
                ROW_NUMBER() OVER (
                    PARTITION BY items.generation_id, items.item_key, sources.source_id
                    ORDER BY (documents.availability = 'available') DESC,
                        documents.created_at, documents.document_id, occurrences.ordinal
                ) AS occurrence_rank
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            JOIN knowledge_generation_item_sources AS sources
                ON sources.generation_id = items.generation_id
                AND sources.item_key = items.item_key
            JOIN evidence_occurrences AS occurrences
                ON occurrences.evidence_id = sources.evidence_id
            JOIN source_documents AS documents
                ON documents.document_id = occurrences.document_id
            JOIN raw_assets AS raw ON raw.asset_sha256 = documents.asset_sha256
            JOIN document_ir_blocks AS blocks ON blocks.block_id = occurrences.block_id
            WHERE state.singleton = 1 AND items.provenance_state = 'source_backed'
        )
        SELECT raw.asset_sha256, raw.raw_path, raw.original_name, raw.media_type,
            documents.display_name, documents.source_format, documents.availability,
            sources.source_id, sources.evidence_id, sources.document_id,
            sources.locator_json
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        JOIN knowledge_page_revision_sources AS sources
            ON sources.revision_id = revisions.revision_id
        JOIN source_documents AS documents ON documents.document_id = sources.document_id
        JOIN raw_assets AS raw ON raw.asset_sha256 = documents.asset_sha256
        WHERE pages.lifecycle_state IN ('stable', 'deprecated')
        UNION ALL
        SELECT asset_sha256, raw_path, original_name, media_type, display_name,
            source_format, availability, source_id, evidence_id, document_id, locator_json
        FROM generated_ranked WHERE occurrence_rank = 1
        ORDER BY asset_sha256, source_id, evidence_id
        """
    ).fetchall()
    grouped: dict[str, tuple[tuple[object, ...], list[_SourceMapping]]] = {}
    for row in rows:
        asset_sha256 = str(row[0])
        mapping = _SourceMapping(str(row[7]), str(row[8]), str(row[9]), _json_object(str(row[10])))
        if asset_sha256 not in grouped:
            grouped[asset_sha256] = (row, [mapping])
        elif mapping not in grouped[asset_sha256][1]:
            grouped[asset_sha256][1].append(mapping)
    return tuple(
        _SourceAsset(
            asset_sha256=asset_sha256,
            raw_path=str(row[1]),
            original_name=str(row[2]),
            media_type=str(row[3]),
            display_name=str(row[4]),
            source_format=str(row[5]),
            availability=str(row[6]),
            mappings=tuple(mappings),
        )
        for asset_sha256, (row, mappings) in grouped.items()
    )


def _source_images_in(
    connection: sqlite3.Connection, mappings: tuple[_SourceMapping, ...]
) -> tuple[_SourceImage, ...]:
    document_ids = tuple(sorted({mapping.document_id for mapping in mappings}))
    if not document_ids:
        return ()
    placeholders = ",".join("?" for _ in document_ids)
    rows = connection.execute(
        f"""
        SELECT source_image_id, document_id, image_sha256, media_type,
            storage_path, display_name, alt_text, locator_json
        FROM source_images WHERE document_id IN ({placeholders})
        ORDER BY document_id, ordinal, source_image_id
        """,
        document_ids,
    ).fetchall()
    locators_by_document: dict[str, list[dict[str, object]]] = {}
    for mapping in mappings:
        locators_by_document.setdefault(mapping.document_id, []).append(mapping.locator)
    selected: list[_SourceImage] = []
    for row in rows:
        source_image_id = str(row[0])
        document_id = str(row[1])
        image_locator = _json_object(str(row[7]))
        if not any(
            source_image_matches_evidence(source_image_id, image_locator, evidence_locator)
            for evidence_locator in locators_by_document.get(document_id, ())
        ):
            continue
        selected.append(
            _SourceImage(
                source_image_id=source_image_id,
                document_id=document_id,
                image_sha256=str(row[2]),
                media_type=str(row[3]),
                storage_path=str(row[4]),
                display_name=str(row[5]),
                alt_text=str(row[6]) if row[6] is not None else None,
            )
        )
    return tuple(selected)


def _manifest(
    *,
    mode: DesktopKnowledgeExportMode,
    sources: tuple[_SourceAsset, ...],
    images: tuple[_SourceImage, ...],
    raw_resources: dict[str, str],
    image_resources: dict[str, str],
) -> dict[str, object]:
    return {
        "format": "openkb-okf-knowledge-bundle-v1",
        "okf_version": "0.2",
        "mode": mode,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sources": [
            {
                "resource": raw_resources.get(
                    source.asset_sha256, f"urn:sha256:{source.asset_sha256}"
                ),
                "asset_sha256": source.asset_sha256,
                "original_name": source.original_name,
                "media_type": source.media_type,
                "display_name": source.display_name,
                "source_format": source.source_format,
                "availability": source.availability,
                "mappings": [
                    {
                        "source_id": mapping.source_id,
                        "evidence_id": mapping.evidence_id,
                        "document_id": mapping.document_id,
                    }
                    for mapping in source.mappings
                ],
            }
            for source in sources
        ],
        "source_images": [
            {
                "resource": image_resources.get(
                    image.source_image_id, f"urn:sha256:{image.image_sha256}"
                ),
                "source_image_id": image.source_image_id,
                "document_id": image.document_id,
                "display_name": image.display_name,
                "media_type": image.media_type,
                "alt_text": image.alt_text,
            }
            for image in images
        ],
    }


def _rewrite_projection_resources(root: Path, resources: dict[str, str]) -> None:
    for path in sorted(root.rglob("*.md")):
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            continue
        prefix, frontmatter, body = content.split("---", 2)
        metadata = yaml.safe_load(frontmatter)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("sources"), list):
            continue
        changed = False
        for source in metadata["sources"]:
            if not isinstance(source, dict):
                continue
            resource = source.get("resource")
            if not isinstance(resource, str) or not resource.startswith("urn:sha256:"):
                continue
            relative = resources.get(resource.removeprefix("urn:sha256:"))
            if relative is None:
                continue
            source["resource"] = os.path.relpath(root / relative, path.parent).replace(os.sep, "/")
            changed = True
        if changed:
            rendered = yaml.safe_dump(
                metadata,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ).rstrip()
            atomic_write_text(path, f"{prefix}---\n{rendered}\n---{body}")


def _stored_file(kb_dir: Path, storage_path: str) -> Path:
    relative = Path(storage_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise DesktopKnowledgeExportError("A referenced stored asset has an invalid path.")
    return kb_dir / relative


def _json_object(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Source locator must be a JSON object.")
    return parsed


def _export_staging_prefix(kb_dir: Path) -> str:
    owner = hashlib.sha256(str(kb_dir).encode("utf-8")).hexdigest()[:16]
    return f".openkb-knowledge-export-{owner}-"


def _discard_abandoned_export_staging(parent: Path, kb_dir: Path) -> None:
    prefix = _export_staging_prefix(kb_dir)
    for candidate in parent.iterdir():
        if candidate.is_dir() and candidate.name.startswith(prefix):
            try:
                shutil.rmtree(candidate)
            except FileNotFoundError:
                # A failed exporter may finish its own best-effort cleanup concurrently.
                pass


def _export_directory_name(mode: DesktopKnowledgeExportMode) -> str:
    label = {
        "knowledge_projection": "Knowledge-Projection",
        "self_contained": "Knowledge-Bundle",
        "portable_wiki": "Portable-Wiki",
    }[mode]
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"OpenKB-{label}-{timestamp}-{uuid.uuid4().hex[:8]}"
