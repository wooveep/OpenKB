"""Integrity-checked Raw Asset access for Desktop source-document readers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_document_parsers import materialize_reader_markdown
from openkb.desktop_import_artifacts import (
    DesktopImportError,
    DocumentIRBlock,
    source_format_uses_structured_ir,
    source_suffixes_for_format,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

_AVAILABLE = "available"
_QUARANTINED = "quarantined"
RAW_DOCUMENT_PAGE_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class DesktopRawDocument:
    """One reader page from a verified complete original."""

    document_id: str
    name: str
    source_format: str
    asset_sha256: str
    byte_size: int
    content: str
    page: int
    has_more: bool
    source_images: tuple["DesktopSourceImage", ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "name": self.name,
            "source_format": self.source_format,
            "asset_sha256": self.asset_sha256,
            "byte_size": self.byte_size,
            "content": self.content,
            "page": self.page,
            "has_more": self.has_more,
            "source_images": [image.as_dict() for image in self.source_images],
        }


@dataclass(frozen=True)
class DesktopSourceImage:
    """One independently saved original image available in the document reader."""

    source_image_id: str
    name: str
    media_type: str
    file_path: str
    alt_text: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_image_id": self.source_image_id,
            "name": self.name,
            "media_type": self.media_type,
            "file_path": self.file_path,
            "alt_text": self.alt_text,
        }


@dataclass(frozen=True)
class _RawAssetRecord:
    document_id: str
    name: str
    source_format: str
    document_availability: str
    asset_sha256: str
    byte_size: int
    raw_path: str
    lifecycle_status: str
    integrity_error_code: str | None


class DesktopRawAssetService:
    """Read originals only after validating their persisted identity and bytes."""

    def __init__(self, kb_dir: Path) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)

    def read_document(self, document_id: str, *, page: int = 0) -> DesktopRawDocument:
        """Return one verified original-reader page without exceeding the Engine frame limit."""
        if page < 0:
            raise DesktopImportError(
                "invalid_raw_document_page", "Original document page must be non-negative."
            )
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                record = self._record_for_document(connection, document_id)
                if record is None:
                    raise DesktopImportError(
                        "document_not_found", "The requested document was not found."
                    )
                if (
                    record.document_availability != _AVAILABLE
                    or record.lifecycle_status != _AVAILABLE
                ):
                    self._quarantine_if_needed(connection, record, record.integrity_error_code)
                    connection.commit()
                    raise DesktopImportError(
                        "raw_asset_quarantined",
                        "The saved original is quarantined and cannot be read.",
                    )
                raw_bytes, error_code = self._validated_bytes(record)
                if error_code is not None:
                    self._quarantine(connection, record, error_code)
                    connection.commit()
                    raise DesktopImportError(error_code, _integrity_message(error_code))
                content = self._reader_content(connection, record, raw_bytes)
                source_images = self._source_images_for_document(connection, record.document_id)
                self._mark_verified(connection, record.asset_sha256)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        content, has_more = _content_page(content, page)
        return DesktopRawDocument(
            document_id=record.document_id,
            name=record.name,
            source_format=record.source_format,
            asset_sha256=record.asset_sha256,
            byte_size=record.byte_size,
            content=content,
            page=page,
            has_more=has_more,
            source_images=source_images,
        )

    def verify_available_documents(self) -> tuple[str, ...]:
        """Quarantine every active document whose complete original no longer verifies."""
        quarantined: list[str] = []
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                records = tuple(self._available_records(connection))
                for record in records:
                    if record.lifecycle_status != _AVAILABLE:
                        self._quarantine_if_needed(connection, record, record.integrity_error_code)
                        quarantined.append(record.document_id)
                        continue
                    _, error_code = self._validated_bytes(record)
                    if error_code is not None:
                        self._quarantine(connection, record, error_code)
                        quarantined.append(record.document_id)
                    else:
                        self._mark_verified(connection, record.asset_sha256)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return tuple(quarantined)

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                f"Not a Desktop Knowledge Base: {self.kb_dir}",
            )
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _record_for_document(
        self, connection: sqlite3.Connection, document_id: str
    ) -> _RawAssetRecord | None:
        row = connection.execute(
            """
            SELECT source_documents.document_id, source_documents.display_name,
                source_documents.source_format, source_documents.availability,
                raw_assets.asset_sha256, raw_assets.byte_size, raw_assets.raw_path,
                raw_asset_integrity.lifecycle_status, raw_asset_integrity.integrity_error_code
            FROM source_documents
            JOIN raw_assets ON raw_assets.asset_sha256 = source_documents.asset_sha256
            JOIN raw_asset_integrity
                ON raw_asset_integrity.asset_sha256 = raw_assets.asset_sha256
            WHERE source_documents.document_id = ?
            """,
            (document_id,),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def _available_records(self, connection: sqlite3.Connection) -> tuple[_RawAssetRecord, ...]:
        rows = connection.execute(
            """
            SELECT source_documents.document_id, source_documents.display_name,
                source_documents.source_format, source_documents.availability,
                raw_assets.asset_sha256, raw_assets.byte_size, raw_assets.raw_path,
                raw_asset_integrity.lifecycle_status, raw_asset_integrity.integrity_error_code
            FROM source_documents
            JOIN raw_assets ON raw_assets.asset_sha256 = source_documents.asset_sha256
            JOIN raw_asset_integrity
                ON raw_asset_integrity.asset_sha256 = raw_assets.asset_sha256
            WHERE source_documents.availability = 'available'
            """
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def _validated_bytes(self, record: _RawAssetRecord) -> tuple[bytes, str | None]:
        if record.raw_path not in _expected_raw_paths(record):
            return b"", "raw_asset_path_invalid"
        try:
            raw_bytes = (self.kb_dir / record.raw_path).read_bytes()
        except FileNotFoundError:
            return b"", "raw_asset_missing"
        except OSError:
            return b"", "raw_asset_unreadable"
        if len(raw_bytes) != record.byte_size:
            return raw_bytes, "raw_asset_size_mismatch"
        if hashlib.sha256(raw_bytes).hexdigest() != record.asset_sha256:
            return raw_bytes, "raw_asset_hash_mismatch"
        return raw_bytes, None

    def _reader_content(
        self,
        connection: sqlite3.Connection,
        record: _RawAssetRecord,
        raw_bytes: bytes,
    ) -> str:
        if record.source_format == "txt":
            try:
                return raw_bytes.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                self._quarantine(connection, record, "raw_asset_content_invalid")
                connection.commit()
                raise DesktopImportError(
                    "raw_asset_content_invalid", "The saved original is not valid text."
                ) from error
        if source_format_uses_structured_ir(record.source_format):
            blocks = self._document_blocks(connection, record.document_id)
            return materialize_reader_markdown(blocks)
        raise DesktopImportError(
            "raw_document_reader_unsupported",
            "This original cannot be displayed by the current Desktop reader.",
        )

    @staticmethod
    def _document_blocks(
        connection: sqlite3.Connection, document_id: str
    ) -> tuple[DocumentIRBlock, ...]:
        rows = connection.execute(
            """
            SELECT block_id, ordinal, kind, text, heading_path, locator_json
            FROM document_ir_blocks
            WHERE document_id = ?
            ORDER BY ordinal
            """,
            (document_id,),
        ).fetchall()
        blocks: list[DocumentIRBlock] = []
        for row in rows:
            try:
                heading_path = json.loads(str(row[4]))
                locator = json.loads(str(row[5]))
            except json.JSONDecodeError as error:
                raise DesktopImportError(
                    "desktop_import_state_invalid", "Document reader source structure is invalid."
                ) from error
            if (
                not isinstance(heading_path, list)
                or not all(isinstance(value, str) for value in heading_path)
                or not isinstance(locator, dict)
            ):
                raise DesktopImportError(
                    "desktop_import_state_invalid", "Document reader source structure is invalid."
                )
            blocks.append(
                DocumentIRBlock(
                    block_id=str(row[0]),
                    ordinal=int(row[1]),
                    kind=str(row[2]),
                    text=str(row[3]),
                    heading_path=tuple(heading_path),
                    line_start=1,
                    line_end=1,
                    locator=locator,
                )
            )
        if not blocks:
            raise DesktopImportError(
                "desktop_import_state_invalid", "Document reader source structure is empty."
            )
        return tuple(blocks)

    def _source_images_for_document(
        self, connection: sqlite3.Connection, document_id: str
    ) -> tuple[DesktopSourceImage, ...]:
        rows = connection.execute(
            """
            SELECT source_image_id, display_name, media_type, storage_path, alt_text
            FROM source_images
            WHERE document_id = ?
            ORDER BY ordinal
            """,
            (document_id,),
        ).fetchall()
        images: list[DesktopSourceImage] = []
        for row in rows:
            storage_path = str(row[3])
            path = self.kb_dir / storage_path
            if not path.is_file():
                continue
            images.append(
                DesktopSourceImage(
                    source_image_id=str(row[0]),
                    name=str(row[1]),
                    media_type=str(row[2]),
                    file_path=str(path.resolve()),
                    alt_text=str(row[4]) if row[4] is not None else None,
                )
            )
        return tuple(images)

    @staticmethod
    def _mark_verified(connection: sqlite3.Connection, asset_sha256: str) -> None:
        connection.execute(
            """
            UPDATE raw_asset_integrity
            SET verified_at = ?, integrity_error_code = NULL
            WHERE asset_sha256 = ?
            """,
            (_timestamp(), asset_sha256),
        )

    @staticmethod
    def _quarantine(
        connection: sqlite3.Connection, record: _RawAssetRecord, error_code: str
    ) -> None:
        connection.execute(
            """
            UPDATE raw_asset_integrity
            SET lifecycle_status = ?, integrity_error_code = ?, verified_at = ?
            WHERE asset_sha256 = ?
            """,
            (_QUARANTINED, error_code, _timestamp(), record.asset_sha256),
        )
        connection.execute(
            """
            UPDATE source_documents
            SET availability = 'failed', available_at = NULL
            WHERE asset_sha256 = ?
            """,
            (record.asset_sha256,),
        )

    @classmethod
    def _quarantine_if_needed(
        cls,
        connection: sqlite3.Connection,
        record: _RawAssetRecord,
        error_code: str | None,
    ) -> None:
        if record.document_availability == _AVAILABLE:
            cls._quarantine(connection, record, error_code or "raw_asset_quarantined")


def _record_from_row(row: tuple[object, ...]) -> _RawAssetRecord:
    return _RawAssetRecord(
        document_id=str(row[0]),
        name=str(row[1]),
        source_format=str(row[2]),
        document_availability=str(row[3]),
        asset_sha256=str(row[4]),
        byte_size=int(str(row[5])),
        raw_path=str(row[6]),
        lifecycle_status=str(row[7]),
        integrity_error_code=str(row[8]) if row[8] is not None else None,
    )


def _expected_raw_paths(record: _RawAssetRecord) -> set[str]:
    try:
        suffixes = source_suffixes_for_format(record.source_format)
    except DesktopImportError:
        return set()
    return {f"raw/{record.asset_sha256}{suffix}" for suffix in suffixes}


def _integrity_message(error_code: str) -> str:
    return {
        "raw_asset_missing": "The saved original is missing.",
        "raw_asset_size_mismatch": "The saved original has an unexpected size.",
        "raw_asset_hash_mismatch": "The saved original no longer matches its recorded hash.",
        "raw_asset_path_invalid": "The saved original has an invalid location record.",
        "raw_asset_unreadable": "The saved original cannot be read.",
    }.get(error_code, "The saved original failed its integrity check.")


def _content_page(content: str, page: int) -> tuple[str, bool]:
    start = 0
    for _ in range(page):
        if start == len(content):
            raise DesktopImportError(
                "raw_document_page_not_found", "Original document page was not found."
            )
        start = _next_page_boundary(content, start)
    end = _next_page_boundary(content, start)
    return content[start:end], end < len(content)


def _next_page_boundary(content: str, start: int) -> int:
    used_bytes = 0
    end = start
    while end < len(content):
        character_size = len(content[end].encode("utf-8"))
        if used_bytes and used_bytes + character_size > RAW_DOCUMENT_PAGE_BYTES:
            break
        used_bytes += character_size
        end += 1
    return end


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
