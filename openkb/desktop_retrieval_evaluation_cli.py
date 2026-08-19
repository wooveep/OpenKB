"""Maintainer CLI for the fixed Desktop retrieval evaluator."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from openkb.desktop_model_transport import desktop_model_gateway_for
from openkb.desktop_retrieval_evaluation import (
    DesktopRetrievalEvaluator,
    EvaluationPageTreeProvider,
)
from openkb.desktop_retrieval_evaluation_types import DesktopRetrievalEvaluationSuite


def run(argv: Sequence[str] | None = None) -> int:
    """Run a JSON suite; a nonzero exit code prevents an unproven expansion."""
    parser = argparse.ArgumentParser(description="Evaluate Desktop vectorless retrieval variants.")
    parser.add_argument("kb_dir", type=Path)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--promote-local-graph", action="store_true")
    parser.add_argument("--validate-page-tree-promotion", action="store_true")
    parser.add_argument("--experimental-pageindex-python", type=Path)
    parser.add_argument("--experimental-pageindex-worker", type=Path)
    parser.add_argument("--pageindex-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--rebuild-official-pageindex", action="store_true")
    args = parser.parse_args(argv)

    if not math.isfinite(args.pageindex_timeout_seconds) or args.pageindex_timeout_seconds <= 0:
        parser.error("--pageindex-timeout-seconds must be positive")
    if (
        args.experimental_pageindex_python is not None
        and args.experimental_pageindex_worker is not None
    ):
        parser.error("select only one experimental PageIndex runtime")
    if (
        args.rebuild_official_pageindex
        and args.experimental_pageindex_python is None
        and args.experimental_pageindex_worker is None
    ):
        parser.error("--rebuild-official-pageindex needs an experimental PageIndex runtime")
    page_tree_provider = _page_tree_provider(args)
    evaluator = DesktopRetrievalEvaluator(
        args.kb_dir,
        model_gateway=desktop_model_gateway_for(args.kb_dir),
        page_tree_provider=cast(EvaluationPageTreeProvider | None, page_tree_provider),
    )
    suite = DesktopRetrievalEvaluationSuite.from_json(args.suite)
    report = evaluator.evaluate(
        suite,
        repetitions=args.repetitions,
        pageindex_worker_sha256=_worker_sha256(args.experimental_pageindex_worker, parser),
    )
    if args.output is None:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        report.write(args.output)
    if args.promote_local_graph and report.local_graph_gate.passed:
        evaluator.promote_local_graph(report)
    if args.validate_page_tree_promotion and report.gate.passed:
        evaluator.require_page_tree_promotion_eligible(report, suite)
    return 0 if report.gate.passed else 2


def _page_tree_provider(args: argparse.Namespace) -> object | None:
    if args.experimental_pageindex_python is None and args.experimental_pageindex_worker is None:
        return None
    from openkb.desktop_pageindex_provider import materialize_official_pageindex_provider

    return materialize_official_pageindex_provider(
        args.kb_dir,
        python_executable=args.experimental_pageindex_python,
        worker_executable=args.experimental_pageindex_worker,
        timeout_seconds=args.pageindex_timeout_seconds,
        force_rebuild=args.rebuild_official_pageindex,
    )


def _worker_sha256(worker: Path | None, parser: argparse.ArgumentParser) -> str | None:
    if worker is None:
        return None
    try:
        return hashlib.sha256(worker.read_bytes()).hexdigest()
    except OSError as error:
        parser.error(f"experimental PageIndex worker is unreadable: {error}")
