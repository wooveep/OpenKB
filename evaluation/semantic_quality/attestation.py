"""Human semantic review validation and digest-only release attestations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluation.semantic_quality.definition import SemanticQualityError

_WINDOWS_SMOKE_SCHEMA_VERSION = "openkb.windows-semantic-smoke.v2"
_WINDOWS_SMOKE_CORPUS = (
    "OCloudView部署手册_V10.2.docx",
    "OCloudView部署手册_V10.3.docx",
)
_WINDOWS_SMOKE_CHECKS = (
    "package_install",
    "document_import",
    "query_planning",
    "knowledge_page_planning",
    "version_comparison",
    "citation_postconditions",
    "candidate_admission",
    "knowledge_graph",
    "grounded_answer",
    "restart_recovery",
    "provider_failure_recovery",
    "semantic_epoch_rejection",
    "privacy_no_secret_leak",
)
_MAX_WINDOWS_SMOKE_REPORT_BYTES = 128 * 1024


def sign_human_attestation(
    run_dir: Path,
    review_path: Path,
    *,
    maintainer: str,
    package_artifact: Path | None = None,
    windows_smoke_report: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    """Bind an explicit per-suite human verdict to one deterministic live run."""
    run_dir = run_dir.resolve()
    report = _artifact_mapping(run_dir / "report.json", "semantic evaluation report")
    pending = _artifact_mapping(
        run_dir / "attestation.pending.json", "pending semantic attestation"
    )
    outputs_path = run_dir / "outputs.jsonl"
    try:
        output_digest = hashlib.sha256(outputs_path.read_bytes()).hexdigest()
        review_bytes = review_path.resolve().read_bytes()
        review = _mapping(json.loads(review_bytes), "human review")
    except (OSError, json.JSONDecodeError) as error:
        raise SemanticQualityError("Cannot read the live outputs or human review.") from error
    if report.get("schema_version") != "openkb.semantic-quality-report.v1":
        raise SemanticQualityError("The semantic evaluation report schema is unsupported.")
    run_id = _text(report.get("run_id"), "report.run_id")
    if pending.get("run_id") != run_id or review.get("run_id") != run_id:
        raise SemanticQualityError("The report, pending attestation, and review run IDs differ.")
    if report.get("status") != "pending_human_review":
        raise SemanticQualityError(
            "Human sign-off is forbidden until deterministic validation passes."
        )
    if report.get("full_pipeline_required") is not True:
        raise SemanticQualityError("Release sign-off requires the production pipeline evaluation.")
    from evaluation.semantic_quality.pipeline import PIPELINE_STAGES

    records = [
        json.loads(line) for line in outputs_path.read_text(encoding="utf-8").splitlines() if line
    ]
    full = [row for row in records if row.get("operation") == "full_pipeline"]
    expected_runs = {
        (suite["case_id"], repetition)
        for suite in report.get("suites", [])
        for repetition in range(1, 4)
    }
    if (
        len(full) != len(expected_runs)
        or {(row.get("case_id"), row.get("repetition")) for row in full} != expected_runs
        or any(
            row.get("valid") is not True
            or row.get("stages") != dict.fromkeys(PIPELINE_STAGES, True)
            for row in full
        )
    ):
        raise SemanticQualityError("Every suite must pass all production stages three times.")
    bindings = _mapping(report.get("bindings"), "report.bindings")
    if bindings.get("output_digest") != output_digest:
        raise SemanticQualityError("The raw evaluation output digest no longer matches the run.")
    if pending.get("bindings") != bindings or pending.get("status") != report.get("status"):
        raise SemanticQualityError("The pending attestation does not match the evaluation report.")
    release_evidence = _release_evidence(
        report,
        bindings,
        package_artifact=package_artifact,
        windows_smoke_report=windows_smoke_report,
    )
    _validate_maintainer(maintainer)
    if review.get("schema_version") != "openkb.semantic-quality-human-review.v1":
        raise SemanticQualityError("The human semantic review schema is unsupported.")

    pending_human = _mapping(pending.get("human_review"), "pending.human_review")
    suite_dimensions = _string_list(
        pending_human.get("suite_dimensions"), "pending suite dimensions"
    )
    pair_dimensions = _string_list(pending_human.get("pair_dimensions"), "pending pair dimensions")
    raw_suites = report.get("suites")
    raw_pairs = report.get("metamorphic_pairs")
    if not isinstance(raw_suites, list) or not isinstance(raw_pairs, list):
        raise SemanticQualityError("The evaluation report omits suite or pair metadata.")
    expected_suite_ids = tuple(
        _text(_mapping(item, "report.suite").get("suite_id"), "report.suite_id")
        for item in raw_suites
    )
    expected_pair_ids = tuple(
        _text(_mapping(item, "report.pair").get("pair_id"), "report.pair_id") for item in raw_pairs
    )
    suites = _review_verdicts(
        review.get("suites"),
        id_field="suite_id",
        expected_ids=expected_suite_ids,
        expected_dimensions=suite_dimensions,
    )
    pairs = _review_verdicts(
        review.get("pairs"),
        id_field="pair_id",
        expected_ids=expected_pair_ids,
        expected_dimensions=pair_dimensions,
    )
    passed = all(item["verdict"] == "pass" for item in (*suites, *pairs))
    attestation = {
        "schema_version": "openkb.semantic-quality-attestation.v1",
        "run_id": run_id,
        "signed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed" if passed else "failed",
        "bindings": bindings,
        "deterministic": {
            "status": "passed",
            "valid_operation_count": report.get("valid_operation_count"),
            "logical_operation_count": report.get("logical_operation_count"),
        },
        "human_review": {
            "review_digest": hashlib.sha256(review_bytes).hexdigest(),
            "maintainer": maintainer.strip(),
            "suites": list(suites),
            "pairs": list(pairs),
        },
    }
    attestation["release_evidence"] = release_evidence
    target = (output_path or run_dir / "attestation.signed.json").resolve()
    _write_new_json(target, attestation)
    return target


def _release_evidence(
    report: dict[str, Any],
    bindings: dict[str, Any],
    *,
    package_artifact: Path | None,
    windows_smoke_report: Path | None,
) -> dict[str, str]:
    if package_artifact is None or windows_smoke_report is None:
        raise SemanticQualityError(
            "Semantic quality sign-off requires a Windows package and its smoke report."
        )
    package_digest = _sha256_file(package_artifact, "Windows package")
    smoke_bytes = _bounded_file_bytes(
        windows_smoke_report,
        "Windows smoke report",
        maximum_bytes=_MAX_WINDOWS_SMOKE_REPORT_BYTES,
    )
    try:
        smoke = _mapping(json.loads(smoke_bytes), "Windows smoke report")
    except json.JSONDecodeError as error:
        raise SemanticQualityError("The Windows smoke report is not valid JSON.") from error
    expected_fields = {
        "schema_version",
        "run_id",
        "platform",
        "status",
        "package_sha256",
        "implementation_digest",
        "matrix_digest",
        "corpus",
        "checks",
    }
    if set(smoke) != expected_fields:
        raise SemanticQualityError("The Windows smoke report has unexpected or missing fields.")
    if smoke.get("schema_version") != _WINDOWS_SMOKE_SCHEMA_VERSION:
        raise SemanticQualityError("The Windows smoke report schema is unsupported.")
    if smoke.get("run_id") != report.get("run_id"):
        raise SemanticQualityError("The Windows smoke report belongs to a different run.")
    if smoke.get("platform") != "windows" or smoke.get("status") != "passed":
        raise SemanticQualityError("The packaged Windows smoke run did not pass.")
    if smoke.get("package_sha256") != package_digest:
        raise SemanticQualityError("The Windows smoke report package digest does not match.")
    if smoke.get("implementation_digest") != bindings.get("implementation_digest"):
        raise SemanticQualityError("The Windows smoke report implementation digest differs.")
    if smoke.get("matrix_digest") != bindings.get("matrix_digest"):
        raise SemanticQualityError("The Windows smoke report matrix digest differs.")
    if smoke.get("corpus") != list(_WINDOWS_SMOKE_CORPUS):
        raise SemanticQualityError("The Windows smoke report did not cover both OCloudView inputs.")
    checks = _mapping(smoke.get("checks"), "Windows smoke checks")
    if set(checks) != set(_WINDOWS_SMOKE_CHECKS) or any(
        checks.get(check) != "passed" for check in _WINDOWS_SMOKE_CHECKS
    ):
        raise SemanticQualityError("Every packaged Windows smoke check must pass.")
    return {
        "package_digest": package_digest,
        "windows_smoke_report_digest": hashlib.sha256(smoke_bytes).hexdigest(),
    }


def _sha256_file(path: Path, name: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.resolve().open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise SemanticQualityError(f"Cannot read the {name}.") from error
    return digest.hexdigest()


def _bounded_file_bytes(path: Path, name: str, *, maximum_bytes: int) -> bytes:
    try:
        resolved = path.resolve()
        if resolved.stat().st_size > maximum_bytes:
            raise SemanticQualityError(f"The {name} exceeds its size limit.")
        return resolved.read_bytes()
    except OSError as error:
        raise SemanticQualityError(f"Cannot read the {name}.") from error


def _validate_maintainer(maintainer: str) -> None:
    if not isinstance(maintainer, str) or not maintainer.strip() or len(maintainer.strip()) > 200:
        raise SemanticQualityError("A bounded maintainer identity is required for sign-off.")
    if any(character in "\r\n" for character in maintainer):
        raise SemanticQualityError("The maintainer identity must be a single line.")


def _review_verdicts(
    value: object,
    *,
    id_field: str,
    expected_ids: tuple[str, ...],
    expected_dimensions: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise SemanticQualityError(f"Human review {id_field} entries must be a list.")
    by_id: dict[str, dict[str, object]] = {}
    for raw_item in value:
        item = _mapping(raw_item, f"human review {id_field}")
        if not set(item).issubset({id_field, "dimensions", "notes"}):
            raise SemanticQualityError(f"Human review {id_field} has unexpected fields.")
        item_id = _text(item.get(id_field), f"human review {id_field}")
        if item_id in by_id:
            raise SemanticQualityError(f"Human review repeats {id_field} {item_id}.")
        dimensions = _mapping(item.get("dimensions"), "human review dimensions")
        if set(dimensions) != set(expected_dimensions):
            raise SemanticQualityError(
                f"Human review {item_id} must decide every rubric dimension exactly once."
            )
        normalized_dimensions: dict[str, object] = {}
        for dimension in expected_dimensions:
            verdict = dimensions.get(dimension)
            if verdict not in {"pass", "fail"}:
                raise SemanticQualityError(
                    f"Human review {item_id}.{dimension} must be pass or fail."
                )
            normalized_dimensions[dimension] = verdict
        by_id[item_id] = {
            id_field: item_id,
            "verdict": (
                "pass"
                if all(verdict == "pass" for verdict in normalized_dimensions.values())
                else "fail"
            ),
            "dimensions": normalized_dimensions,
        }
    if set(by_id) != set(expected_ids):
        raise SemanticQualityError(
            f"Human review must cover every expected {id_field} exactly once."
        )
    return tuple(by_id[item_id] for item_id in expected_ids)


def _write_new_json(path: Path, value: object) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
    except OSError as error:
        raise SemanticQualityError(
            f"Cannot create evaluation artifact without overwriting: {path.name}"
        ) from error


def _artifact_mapping(path: Path, name: str) -> dict[str, Any]:
    try:
        return _mapping(json.loads(path.read_bytes()), name)
    except (OSError, json.JSONDecodeError) as error:
        raise SemanticQualityError(f"Cannot read the {name}.") from error


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SemanticQualityError(f"{field} must be a list.")
    result = tuple(_text(item, field) for item in value)
    if len(set(result)) != len(result):
        raise SemanticQualityError(f"{field} must not contain duplicates.")
    return result


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticQualityError(f"{field} must be an object.")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticQualityError(f"{field} must be non-empty text.")
    return value.strip()
