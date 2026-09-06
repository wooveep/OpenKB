"""Publication gate for one staged Portable Wiki snapshot."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from pathlib import Path

_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_EXTERNAL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def portable_wiki_snapshot_id(snapshot: object) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_portable_wiki(staging: Path) -> None:
    """Reject incomplete routes, aliases, links, resources, or snapshot identity."""
    manifest_path = staging / "wiki-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != "openkb-portable-wiki-v1":
        raise ValueError("Portable Wiki manifest format is invalid.")
    snapshot = manifest.get("snapshot")
    if not isinstance(snapshot, dict) or manifest.get("snapshot_id") != portable_wiki_snapshot_id(
        snapshot
    ):
        raise ValueError("Portable Wiki snapshot identity is invalid.")
    routes = _route_index(manifest.get("routes"), staging)
    _validate_aliases(manifest.get("aliases"), routes)
    _validate_knowledge_sources(manifest.get("routes"), staging)
    _validate_links(staging)
    _validate_resources(manifest.get("source_images"), staging)
    _validate_checksums(manifest.get("checksums"), staging)


def _route_index(value: object, staging: Path) -> dict[str, tuple[str, str]]:
    if not isinstance(value, list):
        raise ValueError("Portable Wiki routes are invalid.")
    routes: dict[str, tuple[str, str]] = {}
    targets: set[tuple[str, str | None]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Portable Wiki route entry is invalid.")
        route, path = item.get("route"), item.get("path")
        identity = item.get("identity")
        if not all(isinstance(field, str) and field for field in (route, path, identity)):
            raise ValueError("Portable Wiki route fields are invalid.")
        assert isinstance(route, str) and isinstance(path, str) and isinstance(identity, str)
        authority, anchor = item.get("authority"), item.get("anchor")
        section_route = authority == "source_section"
        if anchor is not None and not isinstance(anchor, str):
            raise ValueError("Portable Wiki route anchor is invalid.")
        if (
            route in routes
            or (path, anchor) in targets
            or (not section_route and (path != f"{route}.md" or anchor is not None))
            or (section_route and not anchor)
        ):
            raise ValueError("Portable Wiki routes are duplicated or inconsistent.")
        target = _safe_target(staging, path)
        if not target.is_file():
            raise ValueError("Portable Wiki route target is missing.")
        if anchor is not None and f'id="{anchor}"' not in target.read_text(encoding="utf-8"):
            raise ValueError("Portable Wiki route anchor is missing.")
        routes[route] = (identity, path)
        targets.add((path, anchor))
    return routes


def _validate_aliases(value: object, routes: dict[str, tuple[str, str]]) -> None:
    if not isinstance(value, list):
        raise ValueError("Portable Wiki aliases are invalid.")
    aliases: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Portable Wiki alias entry is invalid.")
        alias, identity, route = item.get("alias"), item.get("identity"), item.get("route")
        if not all(isinstance(field, str) and field for field in (alias, identity, route)):
            raise ValueError("Portable Wiki alias fields are invalid.")
        assert isinstance(alias, str) and isinstance(identity, str) and isinstance(route, str)
        normalized = alias.casefold()
        if normalized in aliases or routes.get(route, (None,))[0] != identity:
            raise ValueError("Portable Wiki alias is ambiguous or points to another identity.")
        aliases.add(normalized)


def _validate_knowledge_sources(value: object, staging: Path) -> None:
    if not isinstance(value, list):
        raise ValueError("Portable Wiki routes are invalid.")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Portable Wiki route entry is invalid.")
        if item.get("authority") not in {"user_revision", "published_generation"}:
            continue
        if item.get("kind") not in {"concept", "entity", "procedure"}:
            continue
        path = item.get("path")
        if not isinstance(path, str):
            raise ValueError("Portable Wiki knowledge route is invalid.")
        content = _safe_target(staging, path).read_text(encoding="utf-8")
        _prefix, marker, sources = content.partition("\n## Sources\n")
        links = [match.group(1).strip("<>") for match in _MARKDOWN_LINK.finditer(sources)]
        if not marker or not any("#evidence-" in link for link in links):
            raise ValueError("Portable Wiki knowledge page has no resolvable source bindings.")


def _validate_links(staging: Path) -> None:
    for page in sorted(staging.rglob("*.md")):
        relative_page = page.relative_to(staging).as_posix()
        for match in _MARKDOWN_LINK.finditer(page.read_text(encoding="utf-8")):
            link = match.group(1).strip("<>")
            if _EXTERNAL_SCHEME.match(link):
                continue
            target_value, _separator, fragment = link.partition("#")
            target_relative = target_value or relative_page
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(relative_page), target_relative)
            )
            target = _safe_target(staging, resolved)
            if not target.is_file():
                raise ValueError(f"Portable Wiki link target is missing: {link}")
            if fragment and f'id="{fragment}"' not in target.read_text(encoding="utf-8"):
                raise ValueError(f"Portable Wiki link anchor is missing: {link}")


def _validate_resources(value: object, staging: Path) -> None:
    if not isinstance(value, list):
        raise ValueError("Portable Wiki source images are invalid.")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Portable Wiki source image entry is invalid.")
        resource, expected = item.get("resource"), item.get("sha256")
        if not isinstance(resource, str) or not isinstance(expected, str):
            raise ValueError("Portable Wiki source image fields are invalid.")
        target = _safe_target(staging, resource)
        if not target.is_file() or _sha256(target) != expected:
            raise ValueError("Portable Wiki source image checksum is invalid.")


def _validate_checksums(value: object, staging: Path) -> None:
    if not isinstance(value, dict) or not all(
        isinstance(path, str) and isinstance(digest, str) for path, digest in value.items()
    ):
        raise ValueError("Portable Wiki checksums are invalid.")
    actual_paths = {
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file() and path.name != "wiki-manifest.json"
    }
    if set(value) != actual_paths:
        raise ValueError("Portable Wiki checksum inventory is incomplete.")
    for relative, expected in value.items():
        if _sha256(_safe_target(staging, relative)) != expected:
            raise ValueError("Portable Wiki file checksum is invalid.")


def _safe_target(staging: Path, relative: str) -> Path:
    normalized = posixpath.normpath(relative)
    if (
        not relative
        or "\\" in relative
        or relative.startswith("/")
        or normalized == ".."
        or normalized.startswith("../")
    ):
        raise ValueError("Portable Wiki path escapes its export root.")
    return staging / Path(normalized)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
