"""Acceptance checks for the disposable OKF v0.2 Knowledge Projection."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

import openkb.desktop_okf_projection as okf_projection_module
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_generation_changes_in,
)
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_okf_compatibility import lint_okf_projection, resolve_okf_link
from openkb.desktop_okf_projection import (
    activate_okf_projection,
    canonical_okf_type,
    materialize_okf_projection,
)
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseRuntime,
    desktop_state_database_path,
)


def test_okf_projection_rebuilds_the_current_published_snapshot(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "facts.md"
    source.write_text(
        "# Imported Routing\n\nOriginal evidence for the routing claim.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    evidence = pages.search_sources("Original evidence")[0]

    claim = "OpenKB routes answers through original evidence."
    concept = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Routing",
        content_markdown=claim,
    )
    pages.bind_source(concept.page_id, claim, evidence.evidence_id)
    first_publication = pages.publish(concept.page_id)
    assert first_publication.published_revision is not None
    renamed = pages.save_draft(
        page_id=concept.page_id,
        kind="concept",
        title="Evidence Routing",
        content_markdown=first_publication.published_revision.content_markdown,
    )
    republished = pages.publish(renamed.page_id)
    verified = pages.verify(republished.page_id)

    entity = pages.save_draft(
        page_id=None,
        kind="entity",
        title="OpenKB Desktop",
        content_markdown=(
            f"# Navigation\n\nPlease see [Configuration](../concept/{concept.page_id}.md)."
        ),
    )
    published_entity = pages.publish(entity.page_id)
    pages.set_stale_after(entity.page_id, "2026-08-20T00:00:00+00:00")
    pages.deprecate(entity.page_id)
    pages.save_draft(
        page_id=entity.page_id,
        kind="entity",
        title="OpenKB Desktop Draft",
        content_markdown="UNPUBLISHED PRIVATE DRAFT",
    )

    materialize_okf_projection(kb_dir)
    projection = kb_dir / "knowledge-pages"
    expected_indexes = {
        "index.md",
        "concept/index.md",
        "entity/index.md",
        "generated/index.md",
        "generated/concept/index.md",
        "generated/entity/index.md",
        "log.md",
    }
    assert expected_indexes <= {
        path.relative_to(projection).as_posix() for path in projection.rglob("*.md")
    }

    root_metadata, _ = _markdown_document(projection / "index.md")
    assert root_metadata["okf_version"] == "0.2"
    root_index = (projection / "index.md").read_text(encoding="utf-8")
    assert "](concept/index.md)" in root_index
    assert "](entity/index.md)" in root_index
    assert "](generated/index.md)" in root_index
    assert "](log.md)" in root_index
    assert "](/" not in root_index

    concept_path = projection / "concept" / f"{concept.page_id}.md"
    assert verified.materialized_path == f"knowledge-pages/concept/{concept.page_id}.md"
    concept_metadata, concept_body = _markdown_document(concept_path)
    assert concept_metadata["type"] == "Concept"
    assert concept_metadata["title"] == "Evidence Routing"
    assert concept_metadata["status"] == "stable"
    assert concept_metadata["generated"]["by"] == "openkb-user-revision/1"
    assert concept_metadata["verified"] == [
        {"by": "human:local-user", "at": verified.verification.verified_at}
    ]
    assert concept_metadata["openkb"] == {
        "kind": "Concept",
        "page_id": concept.page_id,
        "revision": 2,
        "authority": "user_revision",
        "provenance": "source_backed",
    }
    assert "page_id" not in concept_metadata
    assert "authority" not in concept_metadata
    assert len(concept_metadata["sources"]) == 1
    projected_source = concept_metadata["sources"][0]
    assert projected_source["id"].startswith("src-")
    assert projected_source["resource"] == (f"urn:sha256:{imported.document.raw_asset_sha256}")
    assert projected_source["openkb"]["canonical_evidence_id"] == evidence.evidence_id
    assert projected_source["openkb"]["document_id"] == imported.document.document_id
    assert isinstance(projected_source["openkb"]["locator"], dict)
    assert claim in concept_body
    assert str(tmp_path) not in concept_path.read_text(encoding="utf-8")

    entity_path = projection / "entity" / f"{entity.page_id}.md"
    assert published_entity.materialized_path == f"knowledge-pages/entity/{entity.page_id}.md"
    entity_metadata, entity_body = _markdown_document(entity_path)
    assert entity_metadata["type"] == "Entity"
    assert entity_metadata["status"] == "deprecated"
    assert entity_metadata["stale_after"] == "2026-08-20T00:00:00+00:00"
    assert entity_metadata["openkb"]["kind"] == "Entity"
    assert "UNPUBLISHED PRIVATE DRAFT" not in entity_body

    generated_pages = tuple(
        path
        for path in (projection / "generated").rglob("*.md")
        if path.name not in {"index.md", "log.md"}
    )
    assert generated_pages
    generated_metadata, _ = _markdown_document(generated_pages[0])
    assert generated_metadata["type"] in {"Concept", "Entity"}
    assert generated_metadata["status"] == "stable"
    assert generated_metadata["openkb"]["authority"] == "published_generation"
    assert "item_key" in generated_metadata["openkb"]
    assert "sources" not in generated_metadata
    assert "verified" not in generated_metadata

    change_log = (projection / "log.md").read_text(encoding="utf-8")
    assert "Knowledge Change Log" in change_log
    assert concept.page_id in change_log
    assert "deprecated" in change_log
    assert "UNPUBLISHED PRIVATE DRAFT" not in change_log
    assert lint_okf_projection(projection) == ()

    snapshot = _projection_snapshot(projection)
    materialize_okf_projection(kb_dir)
    assert _projection_snapshot(projection) == snapshot
    shutil.rmtree(projection)
    materialize_okf_projection(kb_dir)
    assert _projection_snapshot(projection) == snapshot


def test_okf_compatibility_is_permissive_and_resolves_both_link_forms(
    tmp_path: Path,
) -> None:
    assert canonical_okf_type("concept") == "Concept"
    assert canonical_okf_type("entity", "Organization") == "Organization"
    assert canonical_okf_type("entity", "UnknownVendorType") == "Entity"
    bundle = tmp_path / "bundle"
    current = bundle / "entity" / "openkb.md"
    target = bundle / "concept" / "routing.md"
    current.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    (bundle / "index.md").write_text(
        "---\nokf_version: '0.2'\nfuture_root_field: true\n---\n\n# Index\n",
        encoding="utf-8",
    )
    current.write_text(
        "---\ntype: FutureEntitySubtype\nunknown_field: preserved\n---\n\n"
        "See [missing](../missing.md).\n",
        encoding="utf-8",
    )
    target.write_text("---\ntype: Concept\n---\n", encoding="utf-8")

    assert lint_okf_projection(bundle) == ()
    assert resolve_okf_link(bundle, current, "../concept/routing.md#details") == target
    assert resolve_okf_link(bundle, current, "/concept/routing.md") == target

    invalid = bundle / "concept" / "invalid.md"
    invalid.write_text("No frontmatter.\n", encoding="utf-8")
    diagnostics = lint_okf_projection(bundle)
    assert [(item.code, item.path) for item in diagnostics] == [
        ("okf_frontmatter_missing", "concept/invalid.md")
    ]


def test_okf_projection_uses_a_persisted_known_entity_subtype(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "organization.md"
    source.write_text(
        "# Entity: Acme\n\nAcme operates the example service.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    database_path = desktop_state_database_path(kb_dir)
    content = "Acme operates the example service."
    with sqlite3.connect(database_path) as connection:
        current_generation_id = current_generation_id_in(connection)
        assert current_generation_id is not None
        publish_generation_changes_in(
            connection,
            current_generation_id=current_generation_id,
            changes=(
                KnowledgeGenerationChange(
                    document_id=imported.document.document_id,
                    kind="entity",
                    title="Acme",
                    normalized_title="acme",
                    content_markdown=content,
                    content_sha256=knowledge_content_sha256(content),
                    entity_subtype="Organization",
                ),
                KnowledgeGenerationChange(
                    document_id=imported.document.document_id,
                    kind="entity",
                    title="Mystery",
                    normalized_title="mystery",
                    content_markdown="Mystery has an unknown vendor subtype.",
                    content_sha256=knowledge_content_sha256(
                        "Mystery has an unknown vendor subtype."
                    ),
                    entity_subtype="UnknownVendorType",
                ),
            ),
            now="2026-08-19T12:00:00+00:00",
        )
        connection.commit()

    materialize_okf_projection(kb_dir)

    projected_entities = {
        metadata["title"]: metadata
        for path in (kb_dir / "knowledge-pages/generated/entity").glob("*.md")
        if path.name != "index.md"
        for metadata, _body in (_markdown_document(path),)
    }
    assert projected_entities["Acme"]["type"] == "Organization"
    assert projected_entities["Acme"]["openkb"]["kind"] == "Entity"
    assert projected_entities["Mystery"]["type"] == "Entity"
    assert projected_entities["Mystery"]["openkb"]["kind"] == "Entity"


def test_okf_activation_restores_the_previous_bundle_when_the_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    target = kb_dir / "knowledge-pages"
    target.mkdir(parents=True)
    (target / "index.md").write_text("old projection", encoding="utf-8")
    staged = kb_dir / ".openkb" / "okf-projection-staging" / "candidate"
    staged.mkdir(parents=True)
    (staged / "index.md").write_text("new projection", encoding="utf-8")
    real_replace = okf_projection_module.os.replace

    def fail_staged_swap(source: Path | str, destination: Path | str) -> None:
        if Path(source) == staged and Path(destination) == target:
            raise OSError("bundle swap unavailable")
        real_replace(source, destination)

    monkeypatch.setattr(okf_projection_module.os, "replace", fail_staged_swap)

    with pytest.raises(OSError, match="bundle swap unavailable"):
        activate_okf_projection(kb_dir, staged)

    assert (target / "index.md").read_text(encoding="utf-8") == "old projection"
    assert (staged / "index.md").read_text(encoding="utf-8") == "new projection"


def test_okf_activation_does_not_report_post_swap_cleanup_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = tmp_path / "knowledge"
    target = kb_dir / "knowledge-pages"
    target.mkdir(parents=True)
    (target / "index.md").write_text("old projection", encoding="utf-8")
    staged = kb_dir / ".openkb" / "okf-projection-staging" / "candidate"
    staged.mkdir(parents=True)
    (staged / "index.md").write_text("new projection", encoding="utf-8")
    backup_root = kb_dir / ".openkb" / "okf-projection-backups"
    real_rmdir = Path.rmdir

    def fail_backup_root_cleanup(path: Path) -> None:
        if path == backup_root:
            raise PermissionError("backup directory is temporarily locked")
        real_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_backup_root_cleanup)

    activate_okf_projection(kb_dir, staged)

    assert (target / "index.md").read_text(encoding="utf-8") == "new projection"


def _markdown_document(path: Path) -> tuple[dict[str, object], str]:
    content = path.read_text(encoding="utf-8")
    _, frontmatter, body = content.split("---", 2)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed, body.strip()


def _projection_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
