"""Compatibility seam for the complete SQLite-derived OKF projection."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from openkb.knowledge.pages.okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    stage_okf_projection_in,
)

if TYPE_CHECKING:
    from openkb.knowledge.pages.service import DesktopKnowledgePage


def stage_knowledge_page_projection(
    connection: sqlite3.Connection,
    kb_dir: Path,
    _page: DesktopKnowledgePage,
) -> Path:
    """Stage the complete bundle so page and indexes share one snapshot."""
    return stage_okf_projection_in(connection, kb_dir)


def activate_knowledge_page_projection(
    kb_dir: Path, _page: DesktopKnowledgePage, staged: Path
) -> None:
    """Activate the complete bundle after the page publication commits."""
    activate_okf_projection(kb_dir, staged)


def discard_knowledge_page_projection_staging(staged: Path) -> None:
    """Discard a hidden complete-bundle projection."""
    discard_okf_projection_staging(staged)
