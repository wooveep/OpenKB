"""Packaged PageIndex diagnostics and fixed-evaluation boundary validation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_pageindex_adapter import (
    PAGEINDEX_PROVIDER_KIND,
    PAGEINDEX_PROVIDER_VERSION,
    PageIndexProviderError,
    ProviderInvoker,
)
from openkb.desktop_pageindex_provider import (
    PAGEINDEX_CACHE_SCHEMA,
    PageIndexEvaluationProvider,
    detach_official_pageindex_provider_current_for_acceptance,
    materialize_official_pageindex_provider,
)
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_channels import DESKTOP_EVALUATION_VARIANT_ORDER
from openkb.desktop_retrieval_evaluation import recompute_page_tree_evaluation_gate
from openkb.desktop_retrieval_evaluation_types import (
    DesktopRetrievalEvaluationReport,
    DesktopRetrievalEvaluationSuite,
)
from openkb.desktop_retrieval_evaluation_types import (
    evaluation_corpus_identity as pageindex_evaluation_corpus_identity,
)
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseRuntime,
    desktop_state_database_path,
    desktop_state_dir,
)
from openkb.locks import atomic_write_text

_ACCEPTANCE_SCHEMA = 1
_CACHE_TAMPER_TITLE = "OpenKB cache corruption acceptance sentinel"
_PAGEINDEX_ENTRY_POINT = "runtime/pageindex/OpenKBPageIndex.exe"
_PAGEINDEX_EVALUATION_VARIANTS = tuple(
    variant for variant in DESKTOP_EVALUATION_VARIANT_ORDER if variant != "navigator"
)


def run_pageindex_package_acceptance(
    root: Path,
    worker_executable: Path,
    *,
    valid_invoke: ProviderInvoker | None = None,
    timeout_invoke: ProviderInvoker | None = None,
    crash_invoke: ProviderInvoker | None = None,
) -> dict[str, object]:
    """Exercise packaged adapter failures against a disposable real KB."""
    resolved_root = root.expanduser().resolve()
    worker = worker_executable.expanduser().resolve()
    if resolved_root.exists() or not worker.is_file():
        raise ValueError("PageIndex package acceptance paths are invalid.")
    resolved_root.mkdir(parents=True)
    kb_dir = resolved_root / "knowledge"
    source = resolved_root / "pageindex-acceptance.md"
    source.write_text(
        "# Portable PageIndex\n\nPortable PageIndex baseline evidence.\n\n"
        "## Recovery\n\nThe deterministic baseline remains authoritative.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    document_id = imported.document.document_id
    if imported.document.availability != "available":
        raise AssertionError("The disposable package-acceptance document is unavailable.")

    provider = materialize_official_pageindex_provider(
        kb_dir,
        worker_executable=worker,
        invoke=valid_invoke,
    )
    generation_id = _require_provider_generation(provider, kb_dir, document_id)
    _assert_kb_authority(kb_dir, document_id, generation_id)

    timeout_provider = materialize_official_pageindex_provider(
        kb_dir,
        worker_executable=worker,
        timeout_seconds=0.000001,
        force_rebuild=True,
        invoke=timeout_invoke,
    )
    _require_degraded_fallback(
        timeout_provider, kb_dir, document_id, generation_id, "pageindex_provider_timeout"
    )

    invalid_provider = materialize_official_pageindex_provider(
        kb_dir,
        worker_executable=worker,
        force_rebuild=True,
        invoke=_invalid_tree,
    )
    _require_degraded_fallback(
        invalid_provider,
        kb_dir,
        document_id,
        generation_id,
        "pageindex_provider_invalid_tree",
    )

    crash_provider = materialize_official_pageindex_provider(
        kb_dir,
        worker_executable=worker,
        force_rebuild=True,
        invoke=crash_invoke or _crash_worker(worker),
    )
    _require_degraded_fallback(
        crash_provider,
        kb_dir,
        document_id,
        generation_id,
        "pageindex_provider_unavailable",
    )

    cache_dir = (
        desktop_state_dir(kb_dir)
        / "provider-cache"
        / (f"pageindex-{PAGEINDEX_PROVIDER_VERSION.replace('+', '-')}")
    )
    cache_files = tuple(cache_dir.glob("*.json"))
    if len(cache_files) != 1:
        raise AssertionError("The packaged adapter did not create one provider cache entry.")
    _tamper_cache(cache_files[0])
    detach_official_pageindex_provider_current_for_acceptance(kb_dir, document_id)
    repaired = materialize_official_pageindex_provider(
        kb_dir,
        worker_executable=worker,
        invoke=valid_invoke,
    )
    repaired_generation_id = _require_provider_generation(repaired, kb_dir, document_id)
    with repaired.lease(kb_dir, document_id) as repaired_tree:
        if repaired_tree is None or any(
            node.title == _CACHE_TAMPER_TITLE for node in repaired_tree.nodes
        ):
            raise AssertionError("The packaged provider trusted a corrupt cache.")
    _assert_kb_authority(kb_dir, document_id, repaired_generation_id)

    return {
        "schema_version": _ACCEPTANCE_SCHEMA,
        "passed": True,
        "provider_kind": PAGEINDEX_PROVIDER_KIND,
        "provider_version": PAGEINDEX_PROVIDER_VERSION,
        "document_id": document_id,
        "generation_id": generation_id,
        "scenarios": {
            "timeout": "pageindex_provider_timeout",
            "invalid_tree": "pageindex_provider_invalid_tree",
            "cache_corruption": "rebuilt",
            "provider_crash": "pageindex_provider_unavailable",
            "baseline_available": True,
            "sqlite_integrity": True,
        },
    }


def validate_pageindex_evaluation(
    manifest_path: Path, suite_path: Path, report_path: Path
) -> dict[str, object]:
    """Bind a typed fixed report to the exact packaged provider and suite."""
    manifest = _json_object(manifest_path, "Portable package manifest")
    if manifest.get("schemaVersion") != 3:
        raise ValueError("Portable package manifest schema is invalid.")
    experimental = manifest.get("experimentalProviders")
    pageindex = experimental.get("pageIndex") if isinstance(experimental, dict) else None
    evaluation = pageindex.get("evaluation") if isinstance(pageindex, dict) else None
    if not isinstance(pageindex, dict) or not isinstance(evaluation, dict):
        raise ValueError("Portable package PageIndex evaluation identity is missing.")
    if (
        pageindex.get("providerKind") != PAGEINDEX_PROVIDER_KIND
        or pageindex.get("providerVersion") != PAGEINDEX_PROVIDER_VERSION
        or pageindex.get("defaultEnabled") is not False
        or pageindex.get("entryPoint") != _PAGEINDEX_ENTRY_POINT
    ):
        raise ValueError("Portable package PageIndex provider identity is invalid.")

    package_root = manifest_path.expanduser().resolve().parent
    packaged_suite = package_root / "runtime" / "pageindex" / "fixed-suite.json"
    if suite_path.expanduser().resolve() != packaged_suite:
        raise ValueError("Portable package fixed evaluation suite path is invalid.")
    suite = DesktopRetrievalEvaluationSuite.from_json(suite_path)
    corpus_digest, corpus_files = pageindex_evaluation_corpus_identity(suite_path)
    # The frozen PageIndex package report validates that provider, while Navigator is
    # model-backed and measured by the live retrieval evaluation rather than this gate.
    expected_variants = _PAGEINDEX_EVALUATION_VARIANTS
    if (
        evaluation.get("suiteSnapshotId") != suite.snapshot_id
        or evaluation.get("suiteDigest") != suite.digest
        or evaluation.get("caseCount") != len(suite.cases)
        or tuple(evaluation.get("variants", ())) != expected_variants
        or evaluation.get("corpusDigest") != corpus_digest
        or tuple(evaluation.get("corpusFiles", ())) != corpus_files
    ):
        raise ValueError("Portable package fixed evaluation suite identity is invalid.")
    worker_sha256 = _packaged_worker_sha256(manifest, package_root)

    report = DesktopRetrievalEvaluationReport.read(report_path)
    if report.suite_snapshot_id != suite.snapshot_id or report.suite_digest != suite.digest:
        raise ValueError("Fixed evaluation report does not match the packaged suite.")
    if report.corpus_digest != corpus_digest or report.pageindex_worker_sha256 != worker_sha256:
        raise ValueError("Fixed evaluation report does not match the packaged corpus or worker.")
    expected_results = {
        (case.case_id, repetition, variant)
        for case in suite.cases
        for repetition in range(1, report.repetitions + 1)
        for variant in expected_variants
    }
    actual_results = [
        (result.case_id, result.repetition, result.variant) for result in report.results
    ]
    if len(actual_results) != len(set(actual_results)) or set(actual_results) != expected_results:
        raise ValueError("Fixed evaluation report result coverage is invalid.")
    expected_provider = (PAGEINDEX_PROVIDER_KIND, PAGEINDEX_PROVIDER_VERSION)
    if {(item.provider_kind, item.provider_version) for item in report.page_tree_providers} != {
        expected_provider
    }:
        raise ValueError("Fixed evaluation report used a different PageTree provider.")
    if not report.page_tree_generations or any(
        item.provider_kind != expected_provider[0]
        or item.provider_version != expected_provider[1]
        or item.base_generation_id is None
        for item in report.page_tree_generations
    ):
        raise ValueError("Fixed evaluation report generation identity is incomplete.")
    identity_bound = _report_identity_is_bound(report, suite, expected_provider)
    recomputed_gate = recompute_page_tree_evaluation_gate(
        report, suite, derived_identity_bound=identity_bound
    )
    if recomputed_gate != report.gate:
        raise ValueError("Fixed evaluation report gate does not match its measured results.")
    if not recomputed_gate.fixed_suite_complete or not recomputed_gate.derived_identity_bound:
        raise ValueError("Fixed evaluation report is not bound to the complete fixed suite.")
    return {
        "schema_version": _ACCEPTANCE_SCHEMA,
        "valid": True,
        "passed": report.gate.passed,
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "suite_snapshot_id": suite.snapshot_id,
        "suite_digest": suite.digest,
        "corpus_digest": corpus_digest,
        "case_count": len(suite.cases),
        "variant_count": len(expected_variants),
        "repetitions": report.repetitions,
        "provider_kind": expected_provider[0],
        "provider_version": expected_provider[1],
        "worker_sha256": worker_sha256,
    }


def _report_identity_is_bound(
    report: DesktopRetrievalEvaluationReport,
    suite: DesktopRetrievalEvaluationSuite,
    expected_provider: tuple[str, str],
) -> bool:
    generations = report.page_tree_generations
    source_documents = {
        selector.document_name for case in suite.cases for selector in case.expected_evidence
    }
    document_ids = {item.document_id for item in generations}
    generation_ids = {
        item.base_generation_id for item in generations if item.base_generation_id is not None
    }
    providers = {
        (item.provider_kind, item.provider_version)
        for item in generations
        if item.provider_kind is not None and item.provider_version is not None
    }
    result_generation_ids = {
        generation_id
        for result in report.results
        for generation_id in result.page_tree_generation_ids
    }
    result_catalog_ids = {
        generation_id
        for result in report.results
        for generation_id in result.catalog_generation_ids
    }
    cases = {case.case_id: case for case in suite.cases}
    expected_by_case: dict[str, tuple[str, ...]] = {}
    for result in report.results:
        case = cases[result.case_id]
        previous = expected_by_case.setdefault(result.case_id, result.expected_evidence_ids)
        if (
            result.category != case.category
            or result.long_document != case.long_document
            or result.expected_evidence_ids != previous
            or (case.expect_absent_answer and result.expected_evidence_ids)
            or (not case.expect_absent_answer and not result.expected_evidence_ids)
        ):
            return False
    return (
        len(generations) == len(source_documents)
        and len(document_ids) == len(generations)
        and len(generation_ids) == len(generations)
        and providers == {expected_provider}
        and result_generation_ids == generation_ids
        and bool(report.catalog_generation_ids)
        and result_catalog_ids == set(report.catalog_generation_ids)
        and len(report.catalog_generation_ids) == len(set(report.catalog_generation_ids))
        and len(report.knowledge_snapshot_digest) == 64
        and all(character in "0123456789abcdef" for character in report.knowledge_snapshot_digest)
        and report.knowledge_snapshot_revision > 0
    )


def _packaged_worker_sha256(manifest: dict[str, object], package_root: Path) -> str:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("Portable package file inventory is missing.")
    matches = [
        item
        for item in files
        if isinstance(item, dict) and item.get("path") == _PAGEINDEX_ENTRY_POINT
    ]
    if len(matches) != 1:
        raise ValueError("Portable package PageIndex worker inventory is invalid.")
    expected = matches[0].get("sha256")
    worker = package_root.joinpath(*_PAGEINDEX_ENTRY_POINT.split("/"))
    try:
        actual = hashlib.sha256(worker.read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError("Portable package PageIndex worker is unreadable.") from error
    if not isinstance(expected, str) or expected != actual:
        raise ValueError("Portable package PageIndex worker digest is invalid.")
    return actual


def run_cli(argv: list[str]) -> int:
    """Run one explicit frozen-Engine diagnostic without starting stdio RPC."""
    try:
        if len(argv) == 3 and argv[0] == "--pageindex-package-acceptance":
            payload = run_pageindex_package_acceptance(Path(argv[2]), Path(argv[1]))
        elif len(argv) == 4 and argv[0] == "--pageindex-validate-evaluation":
            payload = validate_pageindex_evaluation(Path(argv[1]), Path(argv[2]), Path(argv[3]))
        else:
            raise ValueError("Unsupported OpenKB Engine diagnostic arguments.")
    except Exception as error:
        print(f"OpenKB PageIndex diagnostic failed: {error}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def _require_provider_generation(
    provider: PageIndexEvaluationProvider, kb_dir: Path, document_id: str
) -> str:
    if provider.degradations:
        raise AssertionError("The packaged PageIndex worker did not build its initial tree.")
    with provider.lease(kb_dir, document_id) as generation:
        if generation is None:
            raise AssertionError("The packaged PageIndex provider returned no generation.")
        return generation.generation_id


def _require_degraded_fallback(
    provider: PageIndexEvaluationProvider,
    kb_dir: Path,
    document_id: str,
    generation_id: str,
    error_code: str,
) -> None:
    if error_code not in provider.degradations:
        raise AssertionError(f"The packaged adapter did not record {error_code}.")
    with provider.lease(kb_dir, document_id) as generation:
        if generation is None or generation.generation_id != generation_id:
            raise AssertionError("The packaged adapter did not retain its previous generation.")
    _assert_kb_authority(kb_dir, document_id, generation_id)


def _assert_kb_authority(kb_dir: Path, document_id: str, generation_id: str) -> None:
    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        availability = connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        default_provider = connection.execute(
            """
            SELECT generations.provider_kind FROM document_page_tree_current AS current
            JOIN document_page_tree_generations AS generations
                ON generations.generation_id = current.generation_id
            WHERE current.document_id = ?
            """,
            (document_id,),
        ).fetchone()
        experimental = connection.execute(
            """
            SELECT generation_id FROM document_page_tree_provider_current
            WHERE document_id = ? AND provider_kind = ? AND provider_version = ?
            """,
            (document_id, PAGEINDEX_PROVIDER_KIND, PAGEINDEX_PROVIDER_VERSION),
        ).fetchone()
    if (
        integrity != ("ok",)
        or foreign_keys
        or availability != ("available",)
        or default_provider != ("openkb_deterministic",)
        or experimental != (generation_id,)
    ):
        raise AssertionError("A PageIndex failure changed authoritative KB state.")
    if not DesktopEvidenceRetriever(kb_dir).retrieve("Portable baseline evidence").evidence:
        raise AssertionError("A PageIndex failure removed deterministic baseline evidence.")


def _invalid_tree(input_path: Path, output_path: Path, _timeout: float) -> None:
    line_count = input_path.read_text(encoding="utf-8").count("\n") + 1
    output_path.write_text(
        json.dumps({"line_count": line_count, "structure": [{"title": "Invalid"}]}),
        encoding="utf-8",
    )


def _crash_worker(worker: Path) -> ProviderInvoker:
    def invoke(input_path: Path, output_path: Path, timeout: float) -> None:
        process = subprocess.Popen(
            (str(worker), str(input_path), str(output_path)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=input_path.parent,
            env=_isolated_environment(),
        )
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=min(timeout, 15.0))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        if process.returncode == 0:
            raise AssertionError("The packaged PageIndex crash probe unexpectedly succeeded.")
        raise PageIndexProviderError(
            "pageindex_provider_unavailable", "Packaged PageIndex crash probe failed as expected."
        )

    return invoke


def _tamper_cache(cache_file: Path) -> None:
    payload = _json_object(cache_file, "PageIndex provider cache")
    checkpoint = payload.get("checkpoint")
    if payload.get("schema_version") != PAGEINDEX_CACHE_SCHEMA or not isinstance(checkpoint, dict):
        raise AssertionError("The packaged PageIndex cache shape is invalid.")
    generation = checkpoint.get("generation")
    nodes = generation.get("nodes") if isinstance(generation, dict) else None
    if not isinstance(nodes, list) or len(nodes) < 2 or not isinstance(nodes[1], dict):
        raise AssertionError("The packaged PageIndex cache has no testable node.")
    nodes[1]["title"] = _CACHE_TAMPER_TITLE
    payload["checkpoint_sha256"] = _digest(checkpoint)
    atomic_write_text(
        cache_file,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    )


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is unreadable.") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain an object.")
    return payload


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _isolated_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT")
        if key in os.environ
    }
    system_root = environment.get("SYSTEMROOT", environment.get("WINDIR", ""))
    environment["PATH"] = os.pathsep.join(
        value for value in (str(Path(system_root) / "System32"), system_root) if value
    )
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PIP_NO_INDEX": "1",
            "UV_OFFLINE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    return environment
