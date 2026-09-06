"""Keep shared primitives and Bridge contracts independent of runtime assembly."""

from __future__ import annotations

import ast
from pathlib import Path

import openkb

PACKAGE = Path(openkb.__file__).resolve().parent


def _imports(path: Path) -> set[str]:
    return {
        node.module
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Import)
        for alias in node.names
    }


def test_package_root_remains_a_map_of_domains():
    assert {path.name for path in PACKAGE.glob("*.py")} == {
        "__init__.py",
        "config.py",
        "locks.py",
    }, "Put implementation in its domain package; do not restore flat desktop_* modules."


def test_shared_primitives_do_not_import_domain_implementations():
    files = [*PACKAGE.joinpath("shared").glob("*.py"), *PACKAGE.joinpath("storage").glob("*.py")]
    assert files
    for path in files:
        forbidden = {
            name
            for name in _imports(path)
            if name.startswith("openkb.")
            and not name.startswith(("openkb.shared.", "openkb.storage.", "openkb.locks"))
        }
        assert not forbidden, f"{path.name} depends on domain implementations: {forbidden}"


def test_protocol_does_not_depend_on_engine_assembly():
    imports = _imports(PACKAGE / "engine" / "protocol.py")
    assert not any(name.startswith("openkb.engine") for name in imports)
