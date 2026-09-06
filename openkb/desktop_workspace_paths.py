"""Filesystem layout primitives for one Desktop Knowledge Base."""

from __future__ import annotations

from pathlib import Path

STATE_DIRNAME = ".openkb"
STATE_FILENAME = "state.sqlite3"
INITIALIZING_FILENAME = "initializing"


def resolve_directory(kb_dir: Path) -> Path:
    return kb_dir.expanduser().resolve()


def state_dir(kb_dir: Path) -> Path:
    return kb_dir / STATE_DIRNAME


def state_database_path(kb_dir: Path) -> Path:
    return state_dir(kb_dir) / STATE_FILENAME


def desktop_state_dir(kb_dir: Path) -> Path:
    """Return the Desktop-owned state directory for a known knowledge base."""
    return state_dir(kb_dir)


def desktop_state_database_path(kb_dir: Path) -> Path:
    """Return the SQLite authority path for a known Desktop knowledge base."""
    return state_database_path(kb_dir)


def initialization_marker_path(kb_dir: Path) -> Path:
    return state_dir(kb_dir) / INITIALIZING_FILENAME
