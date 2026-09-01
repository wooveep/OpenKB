"""Acceptance coverage for user-visible Knowledge and Portable Wiki exports."""

from __future__ import annotations

import hashlib
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
from openkb.desktop_portable_wiki_validation import validate_portable_wiki
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

    exported = DesktopKnowledgeExportService(kb_dir).export(destination, mode="self_contained")

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
    assert manifest["sources"][0]["resource"] == (f"raw/{referenced.document.raw_asset_sha256}.md")
    assert len(manifest["source_images"]) == 1
    assert manifest["source_images"][0]["resource"].startswith("images/")
    page_path = root / "concept" / f"{page_id}.md"
    metadata = _frontmatter(page_path)
    resource = metadata["sources"][0]["resource"]
    assert not resource.startswith("urn:")
    assert (page_path.parent / resource).resolve() == referenced_raw.resolve()
    assert unrelated.document.document_id not in json.dumps(manifest)


def test_portable_wiki_export_uses_semantic_routes_and_snapshot_checksums(
    tmp_path: Path,
) -> None:
    kb_dir, imported, page_id = _knowledge_base_with_referenced_image(tmp_path)
    evidence_id = _qualify_portable_wiki_fixture(kb_dir, document_id=imported.document.document_id)
    destination = tmp_path / "exports"
    destination.mkdir()

    exported = _portable_wiki_export(kb_dir, destination)

    root = Path(exported.path)
    assert exported.mode == "portable_wiki"
    assert exported.raw_asset_count == 0
    assert exported.source_image_count == 2
    assert not (root / "raw").exists()
    assert (root / "index.md").is_file()
    assert (root / "summaries" / "index.md").is_file()
    assert (root / "concepts" / "index.md").is_file()
    assert (root / "procedures" / "index.md").is_file()
    assert (root / "sources" / "index.md").is_file()
    assert (root / "summaries" / "referenced.md").is_file()
    assert (root / "concepts" / "routing.md").is_file()
    procedure = root / "generated" / "procedure" / "双节点超融合部署.md"
    assert procedure.is_file()
    assert (root / "sources" / "referenced.md").is_file()
    assert len(tuple((root / "images").iterdir())) == 2
    source_markdown = (root / "sources" / "referenced.md").read_text(encoding="utf-8")
    assert f'<a id="evidence-{evidence_id}"></a>' in source_markdown
    assert "../images/" in source_markdown
    root_index = (root / "index.md").read_text(encoding="utf-8")
    assert "summaries/index.md" in root_index
    assert "sources/index.md" in root_index
    assert "sources/referenced.md" not in root_index
    assert "../../sources/referenced.md#evidence-" in procedure.read_text(encoding="utf-8")

    manifest_path = root / "wiki-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "openkb-portable-wiki-v1"
    expected_snapshot_id = hashlib.sha256(
        json.dumps(manifest["snapshot"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert manifest["snapshot_id"] == expected_snapshot_id
    assert manifest["snapshot"]["knowledge_qualification_state"] == "qualified"
    routes = {entry["route"]: entry for entry in manifest["routes"]}
    assert routes["index"]["authority"] == "navigation_index"
    assert routes["summaries/index"]["identity"] == "index:summary"
    assert routes["procedures/index"]["identity"] == "index:procedure"
    assert routes["summaries/referenced"]["identity"] == imported.document.document_id
    assert routes["concepts/routing"]["identity"] == page_id
    assert routes["generated/procedure/双节点超融合部署"]["identity"] == ("portable-procedure")
    assert routes["sources/referenced"]["identity"] == imported.document.document_id
    section_routes = [
        entry for entry in manifest["routes"] if entry["authority"] == "source_section"
    ]
    assert {entry["title"] for entry in section_routes} == {
        "Routing",
        "Routing / Appendix",
    }
    assert all(entry["path"] == "sources/referenced.md" for entry in section_routes)
    assert all(entry["anchor"].startswith("section-") for entry in section_routes)
    assert all(f'id="{entry["anchor"]}"' in source_markdown for entry in section_routes)
    assert manifest["aliases"] == [
        {
            "alias": "双节点超融合安装",
            "identity": "portable-procedure",
            "route": "generated/procedure/双节点超融合部署",
        }
    ]
    assert "wiki-manifest.json" not in manifest["checksums"]
    for relative, digest in manifest["checksums"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest


def test_portable_wiki_preview_reports_available_documents_without_writing(
    tmp_path: Path,
) -> None:
    kb_dir, imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)
    _qualify_portable_wiki_fixture(kb_dir, document_id=imported.document.document_id)

    preview = DesktopKnowledgeExportService(kb_dir).preview(mode="portable_wiki")

    assert preview.mode == "portable_wiki"
    assert preview.document_count == 1
    assert preview.estimated_size_bytes >= 4096
    assert len(preview.snapshot_id) == 64
    assert not tuple(tmp_path.glob("OpenKB-Portable-Wiki-*"))


def test_portable_wiki_export_rejects_a_previewed_snapshot_after_view_changes(
    tmp_path: Path,
) -> None:
    kb_dir, imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)
    service = DesktopKnowledgeExportService(kb_dir)
    preview = service.preview(mode="portable_wiki")
    destination = tmp_path / "exports"
    destination.mkdir()
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (imported.document.document_id,),
        )
        connection.commit()

    with pytest.raises(DesktopKnowledgeExportError, match="preview has changed"):
        service.export(
            destination,
            mode="portable_wiki",
            expected_snapshot_id=preview.snapshot_id,
        )

    assert not tuple(destination.iterdir())


def test_portable_wiki_reserves_index_routes_from_semantic_title_collisions(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    source = tmp_path / "index.md"
    source.write_text("# Index\n\nSource content.\n", encoding="utf-8")
    imported = DesktopTextImportService(kb_dir).import_text(source)
    destination = tmp_path / "exports"
    destination.mkdir()

    exported = _portable_wiki_export(kb_dir, destination)

    manifest = json.loads((Path(exported.path) / "wiki-manifest.json").read_text(encoding="utf-8"))
    routes = {entry["route"]: entry for entry in manifest["routes"]}
    assert routes["summaries/index"]["identity"] == "index:summary"
    summary_route = next(
        route
        for route, entry in routes.items()
        if entry["kind"] == "summary" and entry["identity"] == imported.document.document_id
    )
    assert summary_route.startswith("summaries/index-")


def test_portable_wiki_rejects_a_broken_internal_link_before_publication(
    tmp_path: Path,
) -> None:
    kb_dir, imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    claim = "Routing is supported by the manual."
    content = f"{claim}\n\nSee [missing page](missing-page.md)."
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Broken route",
        content_markdown=content,
    )
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        evidence_id = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_occurrences WHERE document_id = ? "
                "ORDER BY ordinal LIMIT 1",
                (imported.document.document_id,),
            ).fetchone()[0]
        )
    pages.bind_source(page.page_id, claim, evidence_id)
    pages.publish(page.page_id)
    destination = tmp_path / "exports"
    destination.mkdir()

    with pytest.raises(DesktopKnowledgeExportError):
        _portable_wiki_export(kb_dir, destination)

    assert not tuple(destination.iterdir())


def test_portable_wiki_uses_the_runtime_eligible_user_page_set(tmp_path: Path) -> None:
    kb_dir, imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        evidence_id = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_occurrences WHERE document_id = ? "
                "ORDER BY ordinal LIMIT 1",
                (imported.document.document_id,),
            ).fetchone()[0]
        )
    structural = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Structural only",
        content_markdown="# Navigation",
    )
    pages.publish(structural.page_id)
    deprecated_claim = "A formerly supported fact."
    deprecated = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Deprecated source-backed",
        content_markdown=deprecated_claim,
    )
    pages.bind_source(deprecated.page_id, deprecated_claim, evidence_id)
    pages.publish(deprecated.page_id)
    pages.deprecate(deprecated.page_id)
    destination = tmp_path / "exports"
    destination.mkdir()

    exported = _portable_wiki_export(kb_dir, destination)

    manifest = json.loads((Path(exported.path) / "wiki-manifest.json").read_text(encoding="utf-8"))
    identities = {entry["identity"] for entry in manifest["routes"]}
    assert structural.page_id not in identities
    assert deprecated.page_id not in identities


def test_portable_wiki_excludes_legacy_unqualified_generated_items(tmp_path: Path) -> None:
    kb_dir, imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)
    _insert_legacy_generated_item(kb_dir, document_id=imported.document.document_id)
    destination = tmp_path / "exports"
    destination.mkdir()

    exported = _portable_wiki_export(kb_dir, destination)

    generated = Path(exported.path) / "generated"
    assert not generated.exists() or not tuple(generated.rglob("*.md"))
    manifest = json.loads((Path(exported.path) / "wiki-manifest.json").read_text(encoding="utf-8"))
    assert not any(entry["identity"] == "legacy-generated" for entry in manifest["routes"])


def test_portable_wiki_omits_knowledge_whose_source_bindings_are_unavailable(
    tmp_path: Path,
) -> None:
    kb_dir, imported, page_id = _knowledge_base_with_referenced_image(tmp_path)
    _qualify_portable_wiki_fixture(kb_dir, document_id=imported.document.document_id)
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (imported.document.document_id,),
        )
        connection.commit()
    destination = tmp_path / "exports"
    destination.mkdir()

    exported = _portable_wiki_export(kb_dir, destination)

    root = Path(exported.path)
    manifest = json.loads((root / "wiki-manifest.json").read_text(encoding="utf-8"))
    identities = {entry["identity"] for entry in manifest["routes"]}
    assert page_id not in identities
    assert "portable-procedure" not in identities
    content = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.md"))
    assert "初始化双节点超融合环境" not in content


def test_portable_wiki_validator_rejects_a_knowledge_page_without_sources(
    tmp_path: Path,
) -> None:
    kb_dir, imported, _page_id = _knowledge_base_with_referenced_image(tmp_path)
    _qualify_portable_wiki_fixture(kb_dir, document_id=imported.document.document_id)
    destination = tmp_path / "exports"
    destination.mkdir()
    root = Path(_portable_wiki_export(kb_dir, destination).path)
    manifest_path = root / "wiki-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    procedure = next(
        entry
        for entry in manifest["routes"]
        if entry["authority"] == "published_generation" and entry["kind"] == "procedure"
    )
    page_path = root / procedure["path"]
    page_path.write_text(
        page_path.read_text(encoding="utf-8").split("\n## Sources\n", maxsplit=1)[0] + "\n",
        encoding="utf-8",
    )
    manifest["checksums"][procedure["path"]] = hashlib.sha256(page_path.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source bindings"):
        validate_portable_wiki(root)


def test_portable_wiki_disambiguates_same_named_document_versions(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "manual.md"
    second = second_dir / "manual.md"
    first.write_text("# First\n\nFirst document version.", encoding="utf-8")
    second.write_text("# Second\n\nSecond document version.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(first)
    DesktopTextImportService(kb_dir).import_text(second)
    destination = tmp_path / "exports"
    destination.mkdir()

    exported = _portable_wiki_export(kb_dir, destination)

    root = Path(exported.path)
    manifest = json.loads((root / "wiki-manifest.json").read_text(encoding="utf-8"))
    source_routes = [
        entry for entry in manifest["routes"] if entry["authority"] == "source_document"
    ]
    assert len(source_routes) == 2
    assert len({entry["route"] for entry in source_routes}) == 2
    assert all((root / entry["path"]).is_file() for entry in source_routes)


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


def _portable_wiki_export(kb_dir: Path, destination: Path):
    service = DesktopKnowledgeExportService(kb_dir)
    preview = service.preview(mode="portable_wiki")
    return service.export(
        destination,
        mode="portable_wiki",
        expected_snapshot_id=preview.snapshot_id,
    )


def _qualify_portable_wiki_fixture(kb_dir: Path, *, document_id: str) -> str:
    database = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database) as connection:
        evidence_id = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_occurrences "
                "WHERE document_id = ? ORDER BY ordinal LIMIT 1",
                (document_id,),
            ).fetchone()[0]
        )
        generation_id = int(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE knowledge_generations "
            "SET qualification_state = 'qualified', synthesis_schema_version = ? "
            "WHERE generation_id = ?",
            ("openkb.corpus-knowledge.v1", generation_id),
        )
        connection.execute(
            """
            INSERT INTO document_summaries (
                document_id, provenance_state, section_map_json,
                analysis_provenance_json, created_at, updated_at
            ) VALUES (?, 'source_backed', '[]', '{}', ?, ?)
            """,
            (document_id, "2026-08-31T00:00:00Z", "2026-08-31T00:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO document_summary_units (
                document_id, unit_ordinal, role, unit_text
            ) VALUES (?, 0, 'purpose', ?)
            """,
            (document_id, "本文说明路由与双节点部署资料。"),
        )
        connection.execute(
            """
            INSERT INTO document_summary_unit_sources (
                document_id, unit_ordinal, evidence_id
            ) VALUES (?, 0, ?)
            """,
            (document_id, evidence_id),
        )
        content = "## 操作步骤\n\n1. 初始化双节点超融合环境。"
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype, analysis_provenance_json,
                aliases_json, tags_json, identity_id
            ) VALUES (?, 'portable-procedure', 'procedure', ?, ?, ?, ?, ?, ?,
                'source_backed', NULL, '{}', ?, '[]', 'portable-identity')
            """,
            (
                generation_id,
                "双节点超融合部署",
                "双节点超融合部署",
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                document_id,
                "2026-08-31T00:00:00Z",
                json.dumps(["双节点超融合安装"], ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_generation_item_sources (
                generation_id, item_key, source_id, evidence_id, claim_text
            ) VALUES (?, 'portable-procedure', 'portable-source', ?, ?)
            """,
            (generation_id, evidence_id, "初始化双节点超融合环境。"),
        )
        return evidence_id


def _insert_legacy_generated_item(kb_dir: Path, *, document_id: str) -> None:
    content = "Legacy content must stay outside portable navigation."
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        generation_id = int(
            connection.execute(
                "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype, analysis_provenance_json,
                aliases_json, tags_json, identity_id
            ) VALUES (?, 'legacy-generated', 'concept', 'Legacy Generated',
                'legacy generated', ?, ?, ?, ?, 'source_backed', NULL, '{}',
                '[]', '[]', 'legacy-identity')
            """,
            (
                generation_id,
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                document_id,
                "2026-08-31T00:00:00Z",
            ),
        )


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
