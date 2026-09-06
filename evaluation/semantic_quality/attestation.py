"""Human semantic review validation and digest-only release attestations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluation.semantic_quality.definition import SemanticQualityError


def sign_human_attestation(
    run_dir: Path,
    review_path: Path,
    *,
    maintainer: str,
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
    bindings = _mapping(report.get("bindings"), "report.bindings")
    if bindings.get("output_digest") != output_digest:
        raise SemanticQualityError("The raw evaluation output digest no longer matches the run.")
    if pending.get("bindings") != bindings or pending.get("status") != report.get("status"):
        raise SemanticQualityError("The pending attestation does not match the evaluation report.")
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
    target = (output_path or run_dir / "attestation.signed.json").resolve()
    _write_new_json(target, attestation)
    return target


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
