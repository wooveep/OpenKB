"""Acceptance coverage for both user-visible OKF Knowledge Bundle exports."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

import openkb.desktop_knowledge_export as knowledge_export_module
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_export import (
    DesktopKnowledgeExportError,
    DesktopKnowledgeExportService,
)
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_knowledge_projection_export_contains_okf_and_manifest_without_raw_assets(
    tmp_path: Path,
) -> None:
    kb_dir, imported, page_id = _knowledge_base_with_referenced_image(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    deprecated = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Historical Router",
        content_markdown="Please see [Configuration](configuration.md).",
    )
    pages.publish(deprecated.page_id)
    pages.deprecate(deprecated.page_id)
    pages.save_draft(
        page_id=deprecated.page_id,
        kind="entity",
        title="Private Draft",
        content_markdown="UNPUBLISHED PRIVATE CONTENT",
    )
    before = _authority_snapshot(kb_dir)
    destination = tmp_path / "exports"
    destination.mkdir()

    exported = DesktopKnowledgeExportService(kb_dir).export(
        destination, mode="knowledge_projection"
    )

    root = Path(exported.path)
    assert root.parent == destination
    assert exported.mode == "knowledge_projection"
    assert (root / "index.md").is_file()
    assert (root / "log.md").is_file()
    assert (root / "source-manifest.json").is_file()
    assert not (root / "raw").exists()
    assert not (root / "images").exists()
    assert "UNPUBLISHED PRIVATE CONTENT" not in "".join(
        path.read_text(encoding="utf-8") for path in root.rglob("*.md")
    )
    assert (root / "entity" / f"{deprecated.page_id}.md").is_file()
    manifest_text = (root / "source-manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["mode"] == "knowledge_projection"
    assert manifest["sources"] == [
        {
            "asset_sha256": imported.document.raw_asset_sha256,
            "availability": "available",
            "display_name": "referenced.md",
            "media_type": "text/markdown",
            "mappings": [
                {
                    "document_id": imported.document.document_id,
                    "evidence_id": manifest["sources"][0]["mappings"][0]["evidence_id"],
                    "source_id": manifest["sources"][0]["mappings"][0]["source_id"],
                }
            ],
            "original_name": "referenced.md",
            "resource": f"urn:sha256:{imported.document.raw_asset_sha256}",
            "source_format": "markdown",
        }
    ]
    assert str(tmp_path) not in manifest_text
    assert page_id in (root / "concept" / f"{page_id}.md").read_text(encoding="utf-8")
    assert _authority_snapshot(kb_dir) == before


def test_self_contained_export_copies_only_referenced_raw_and_images_and_rewrites_sources(
    tmp_path: Path,
) -> None:
    kb_dir, referenced, page_id = _knowledge_base_with_referenced_image(tmp_path)
    unrelated_source = tmp_path / "unrelated.md"
    unrelated_image = tmp_path / "unrelated.png"
    unrelated_image.write_bytes(b"\x89PNG\r\n\x1a\nunrelated")
    unrelated_source.write_text(
        "# Unrelated\n\nUnrelated evidence.\n\n![Unrelated](unrelated.png)\n",
        encoding="utf-8",
    )
    unrelated = DesktopTextImportService(kb_dir).import_text(unrelated_source)
    destination = tmp_path / "exports"
    destination.mkdir()

    exported = DesktopKnowledgeExportService(kb_dir).export(
        destination, mode="self_contained"
    )

    root = Path(exported.path)
    referenced_raw = root / "raw" / f"{referenced.document.raw_asset_sha256}.md"
    unrelated_raw = root / "raw" / f"{unrelated.document.raw_asset_sha256}.md"
    assert referenced_raw.read_bytes() == (tmp_path / "referenced.md").read_bytes()
    assert not unrelated_raw.exists()
    images = tuple(path for path in (root / "images").iterdir() if path.is_file())
    assert len(images) == 1
    assert images[0].read_bytes() == b"\x89PNG\r\n\x1a\nreferenced"
    manifest = json.loads((root / "source-manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "self_contained"
    assert manifest["sources"][0]["resource"] == (
        f"raw/{referenced.document.raw_asset_sha256}.md"
    )
    assert len(manifest["source_images"]) == 1
    assert manifest["source_images"][0]["resource"].startswith("images/")
    page_path = root / "concept" / f"{page_id}.md"
    metadata = _frontmatter(page_path)
    resource = metadata["sources"][0]["resource"]
    assert not resource.startswith("urn:")
    assert (page_path.parent / resource).resolve() == referenced_raw.resolve()
    assert unrelated.document.document_id not in json.dumps(manifest)


def test_export_failure_leaves_no_partial_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir, _imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)
    destination = tmp_path / "exports"
    destination.mkdir()

    def fail_copy(_source: Path | str, _destination: Path | str) -> None:
        raise OSError("destination became unavailable")

    monkeypatch.setattr(knowledge_export_module.shutil, "copy2", fail_copy)

    with pytest.raises(DesktopKnowledgeExportError) as failure:
        DesktopKnowledgeExportService(kb_dir).export(destination, mode="self_contained")

    assert failure.value.code == "knowledge_export_failed"
    assert tuple(destination.iterdir()) == ()


def test_export_rejects_destination_inside_active_knowledge_base(tmp_path: Path) -> None:
    kb_dir, _imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)

    with pytest.raises(DesktopKnowledgeExportError) as failure:
        DesktopKnowledgeExportService(kb_dir).export(
            kb_dir / "knowledge-pages", mode="knowledge_projection"
        )

    assert failure.value.code == "knowledge_export_failed"


def test_export_removes_only_this_knowledge_base_abandoned_staging(tmp_path: Path) -> None:
    kb_dir, _imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)
    destination = tmp_path / "exports"
    destination.mkdir()
    abandoned = destination / f"{knowledge_export_module._export_staging_prefix(kb_dir)}old"
    abandoned.mkdir()
    (abandoned / "partial").write_text("partial", encoding="utf-8")
    other_owner = destination / ".openkb-knowledge-export-other-owner-active"
    other_owner.mkdir()

    exported = DesktopKnowledgeExportService(kb_dir).export(
        destination, mode="knowledge_projection"
    )

    assert not abandoned.exists()
    assert other_owner.is_dir()
    assert Path(exported.path).is_dir()


def test_abandoned_staging_cleanup_is_idempotent_when_another_exporter_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    destination = tmp_path / "exports"
    destination.mkdir()
    abandoned = destination / f"{knowledge_export_module._export_staging_prefix(kb_dir)}old"
    abandoned.mkdir()
    real_rmtree = knowledge_export_module.shutil.rmtree

    def concurrent_cleanup(path: Path) -> None:
        real_rmtree(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(knowledge_export_module.shutil, "rmtree", concurrent_cleanup)

    knowledge_export_module._discard_abandoned_export_staging(destination, kb_dir)

    assert not abandoned.exists()


def _knowledge_base_with_referenced_image(tmp_path: Path) -> tuple[Path, object, str]:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "referenced.md"
    image = tmp_path / "referenced.png"
    other_image = tmp_path / "same-document-unrelated.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreferenced")
    other_image.write_bytes(b"\x89PNG\r\n\x1a\nsame-document-unrelated")
    source.write_text(
        "# Routing\n\n![Routing diagram](referenced.png)\n\n"
        "## Appendix\n\n![Unrelated diagram](same-document-unrelated.png)\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    evidence = pages.search_sources("Routing diagram")[0]
    claim = "OpenKB uses the referenced routing diagram."
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Routing",
        content_markdown=claim,
    )
    pages.bind_source(page.page_id, claim, evidence.evidence_id)
    pages.publish(page.page_id)
    return kb_dir, imported, page.page_id


def _authority_snapshot(kb_dir: Path) -> tuple[list[tuple[object, ...]], ...]:
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        return tuple(
            connection.execute(query).fetchall()
            for query in (
                "SELECT page_id, current_revision_id, lifecycle_state "
                "FROM knowledge_pages ORDER BY page_id",
                "SELECT revision_id, content_markdown "
                "FROM knowledge_page_revisions ORDER BY revision_id",
                "SELECT document_id, availability FROM source_documents ORDER BY document_id",
                "SELECT current_generation_id FROM knowledge_generation_state",
            )
        )


def _frontmatter(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    _, frontmatter, _body = content.split("---", 2)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed
