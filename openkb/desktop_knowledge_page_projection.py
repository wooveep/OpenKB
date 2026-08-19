"""Crash-safe filesystem projection for SQLite Knowledge Page revisions."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from openkb.locks import atomic_write_text

if TYPE_CHECKING:
    from openkb.desktop_knowledge_pages import DesktopKnowledgePage


def stage_knowledge_page_projection(kb_dir: Path, page: DesktopKnowledgePage) -> Path:
    staging_root = kb_dir / "knowledge-pages" / ".page-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staged = staging_root / f"{uuid.uuid4().hex}.md"
    atomic_write_text(staged, render_knowledge_page_markdown(page))
    return staged


def activate_knowledge_page_projection(
    kb_dir: Path, page: DesktopKnowledgePage, staged: Path
) -> None:
    target = kb_dir / page.materialized_path
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, target)


def discard_knowledge_page_projection_staging(staged: Path) -> None:
    staged.unlink(missing_ok=True)


def discard_abandoned_knowledge_page_projection_staging(kb_dir: Path) -> None:
    staging_root = kb_dir / "knowledge-pages" / ".page-staging"
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)


def render_knowledge_page_markdown(page: DesktopKnowledgePage) -> str:
    published = page.published_revision
    if published is None:
        raise ValueError("A Working Draft cannot be materialized.")
    frontmatter = "\n".join(
        (
            "---",
            f"page_id: {json.dumps(page.page_id, ensure_ascii=False)}",
            f"kind: {json.dumps(page.kind, ensure_ascii=False)}",
            f"title: {json.dumps(published.title, ensure_ascii=False)}",
            f"revision: {published.revision_number}",
            'authority: "user_revision"',
            f"openkb.provenance: {json.dumps(published.provenance_state)}",
            f"updated_at: {json.dumps(published.published_at, ensure_ascii=False)}",
            "---",
        )
    )
    body = published.content_markdown.rstrip("\n")
    footnotes = "\n".join(
        f"[^{source.source_id}]: {source.document_name} / {source.section}".rstrip(" /")
        for source in published.source_map
    )
    suffix = f"\n\n{footnotes}" if footnotes else ""
    return f"{frontmatter}\n\n# {published.title}\n\n{body}{suffix}\n"
