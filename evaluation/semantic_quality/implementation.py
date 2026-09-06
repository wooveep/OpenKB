"""Bind release evidence to the complete production source and packaging inputs."""

from __future__ import annotations

import hashlib
from pathlib import Path

from evaluation.semantic_quality.definition import SemanticQualityError
from openkb.shared.canonical_json import canonical_json_digest

_TREES = {
    "openkb": {".py", ".json", ".sql", ".yaml", ".yml"},
    "evaluation": {".py", ".json"},
    "frontend/src": {".ts", ".tsx", ".js", ".mjs", ".css", ".json", ".svg"},
    "frontend/scripts": {".mjs", ".js"},
    "desktop/src-tauri/src": {".rs"},
    "desktop/src-tauri/capabilities": {".json"},
    "desktop/scripts": {".ps1", ".py"},
    ".github/workflows": {".yml", ".yaml"},
}
_FILES = (
    "pyproject.toml",
    "uv.lock",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/vite.config.ts",
    "frontend/tsconfig.json",
    "frontend/tsconfig.app.json",
    "frontend/index.html",
    "desktop/src-tauri/Cargo.toml",
    "desktop/src-tauri/Cargo.lock",
    "desktop/src-tauri/build.rs",
    "desktop/src-tauri/tauri.conf.json",
)


def implementation_digest(repository_root: Path) -> str:
    """Include new/deleted source files automatically; exclude secrets and build outputs."""
    root = repository_root.resolve()
    paths: set[Path] = set()
    for tree, suffixes in _TREES.items():
        directory = root / tree
        if not directory.is_dir():
            raise SemanticQualityError(f"Missing release implementation directory: {tree}")
        paths.update(p for p in directory.rglob("*") if p.is_file() and p.suffix in suffixes)
    paths.update(root / name for name in _FILES)
    try:
        manifest = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)
        }
    except OSError as error:
        raise SemanticQualityError("Cannot bind the complete release implementation.") from error
    return canonical_json_digest(manifest)
