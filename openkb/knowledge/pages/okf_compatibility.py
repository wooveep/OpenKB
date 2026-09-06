"""Permissive OKF v0.2 projection checks and local-link resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

_RESERVED_MARKDOWN_NAMES = frozenset({"index.md", "log.md"})


@dataclass(frozen=True)
class OkfCompatibilityDiagnostic:
    """One minimal format problem; OpenKB Publication Gate errors live elsewhere."""

    code: str
    path: str
    message: str


def lint_okf_projection(bundle_root: Path) -> tuple[OkfCompatibilityDiagnostic, ...]:
    """Check only OKF's small compatibility boundary.

    Unknown fields and types, missing optional metadata, and broken links are
    deliberately accepted.  Publication eligibility is a separate OpenKB rule.
    """
    root = bundle_root.expanduser().resolve()
    diagnostics: list[OkfCompatibilityDiagnostic] = []
    if not root.is_dir():
        return (
            OkfCompatibilityDiagnostic(
                "okf_bundle_not_found", ".", "The OKF bundle directory does not exist."
            ),
        )
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        metadata, error = _frontmatter(path)
        if error is not None:
            if path.name in _RESERVED_MARKDOWN_NAMES and error == "okf_frontmatter_missing":
                continue
            diagnostics.append(_diagnostic(error, relative))
            continue
        if path.name in _RESERVED_MARKDOWN_NAMES:
            continue
        assert metadata is not None
        document_type = metadata.get("type")
        if not isinstance(document_type, str) or not document_type.strip():
            diagnostics.append(_diagnostic("okf_type_required", relative))
    return tuple(diagnostics)


def resolve_okf_link(bundle_root: Path, current_document: Path, target: str) -> Path | None:
    """Resolve ordinary relative and OKF bundle-root links to one local path."""
    parsed = urlsplit(target.strip())
    if parsed.scheme or parsed.netloc:
        return None
    root = bundle_root.expanduser().resolve()
    current = current_document.expanduser().resolve()
    link_path = unquote(parsed.path).replace("\\", "/")
    candidate = (
        root / link_path.lstrip("/") if link_path.startswith("/") else current.parent / link_path
    ).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("OKF link leaves the bundle root.") from error
    return candidate


def _frontmatter(path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None, "okf_markdown_unreadable"
    if not lines or lines[0].strip() != "---":
        return None, "okf_frontmatter_missing"
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        return None, "okf_frontmatter_invalid"
    try:
        parsed = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError:
        return None, "okf_frontmatter_invalid"
    if not isinstance(parsed, dict):
        return None, "okf_frontmatter_invalid"
    return {str(key): value for key, value in parsed.items()}, None


def _diagnostic(code: str, path: str) -> OkfCompatibilityDiagnostic:
    messages = {
        "okf_frontmatter_missing": "Concept Markdown requires YAML frontmatter.",
        "okf_frontmatter_invalid": "The YAML frontmatter could not be parsed.",
        "okf_markdown_unreadable": "The Markdown file is not readable UTF-8 text.",
        "okf_type_required": "Concept frontmatter requires a non-empty type.",
    }
    return OkfCompatibilityDiagnostic(code, path, messages[code])
