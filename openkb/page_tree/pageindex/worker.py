"""Minimal isolated worker for the pinned official PageIndex Markdown builder."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import sys
import types
from pathlib import Path

EXPECTED_PAGEINDEX_VERSION = "0.2.10"


def _load_md_to_tree():
    distribution = importlib.metadata.distribution("pageindex")
    if distribution.version != EXPECTED_PAGEINDEX_VERSION:
        raise RuntimeError("The official PageIndex runtime version does not match the lock.")
    package_path = Path(distribution.locate_file("pageindex")).resolve()
    package = types.ModuleType("pageindex")
    package.__path__ = [str(package_path)]  # type: ignore[attr-defined]
    package.__package__ = "pageindex"
    sys.modules["pageindex"] = package
    module = importlib.import_module("pageindex.page_index_md")
    return module.md_to_tree


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        _load_md_to_tree()
        print(json.dumps({"pageindex_version": EXPECTED_PAGEINDEX_VERSION}))
        return 0
    if len(argv) != 2:
        raise RuntimeError("PageIndex worker needs one input and one output path.")
    input_path, output_path = (Path(value).resolve() for value in argv)
    md_to_tree = _load_md_to_tree()
    result = asyncio.run(
        md_to_tree(
            input_path,
            if_thinning=False,
            if_add_node_summary="no",
            if_add_doc_description="no",
            if_add_node_text="no",
            if_add_node_id="yes",
        )
    )
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
