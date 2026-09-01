"""Validated release attestation for the maintainer-local fixed real corpus."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from openkb.desktop_prompt_contracts import prompt_contract_for

REAL_CORPUS_ATTESTATION_SCHEMA_VERSION = "openkb.real-corpus-attestation.v2"
REAL_CORPUS_IMPLEMENTATION_VERSION = "openkb.knowledge-quality-pipeline.v1"
REAL_CORPUS_SUITE_ID = "ocloudware-dual-node-hyperconverged-v1"
_ATTESTATION_PATH = Path(__file__).with_name("benchmarks") / "real-corpus-attestation.json"
_IMPLEMENTATION_MANIFEST_PATH = (
    Path(__file__).with_name("benchmarks") / "real-corpus-implementation.json"
)
_MAX_ORIGINAL_COMPLETENESS_DELTA = 0.1
_PORTABLE_DIGEST_EXCLUDED_PATHS = frozenset({"openkb.local.json", "release-manifest.json"})
_PORTABLE_ATTESTATION_SUFFIX = "/openkb/benchmarks/real-corpus-attestation.json"
_CONTRACT_OPERATIONS = (
    "knowledge_analysis",
    "knowledge_analysis_batch",
    "knowledge_analysis_merge",
    "retrieval_plan",
    "page_tree_selection",
    "knowledge_navigation_step",
    "grounded_answer",
)


@dataclass(frozen=True)
class RealCorpusCaseResult:
    case_id: str
    repetitions: int
    answer_completeness: float
    answer_correctness: float
    citation_precision: float
    unsupported_claim_count: int
    degradation_runs: int
    retrieval_replay_passed: bool
    original_answer_completeness: float
    original_answer_correctness: float
    original_citation_precision: float
    original_degradation_runs: int
    original_comparison_passed: bool


@dataclass(frozen=True)
class RealCorpusOriginalBaseline:
    commit_sha: str
    model_profile_digest: str
    sample_count: int
    answer_completeness: float
    answer_correctness: float
    citation_precision: float
    degradation_runs: int
    fallback_runs: int


@dataclass(frozen=True)
class RealCorpusWindowsAcceptance:
    platform: str
    host_os: str
    artifact_kind: str
    artifact_digest: str
    package_manifest_digest: str
    questions_run: int
    degradation_runs: int
    packaged_smoke_passed: bool
    cancellation_passed: bool
    cancellation_interruption_code: str
    regeneration_completed: bool
    restart_readable: bool
    answer_versions_preserved: bool
    answer_version_count: int


@dataclass(frozen=True)
class RealCorpusBenchmarkAttestation:
    schema_version: str
    suite_id: str
    implementation_version: str
    implementation_digest: str
    implementation_commit_sha: str
    corpus_digest: str
    contract_digest: str
    model_profile_digest: str
    report_digest: str
    collected_at: str
    source_document_count: int
    sample_count: int
    answer_completeness: float
    answer_correctness: float
    citation_precision: float
    unsupported_claim_count: int
    degradation_runs: int
    noise_leakage_rate: float
    duplicate_identity_rate: float
    multi_document_topic_coverage: float
    procedure_stage_coverage: float
    retrieval_replay_passed: bool
    automated_regression_passed: bool
    passed: bool
    original_baseline: RealCorpusOriginalBaseline
    windows_acceptance: RealCorpusWindowsAcceptance
    cases: tuple[RealCorpusCaseResult, ...] = ()
    failure_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def current_real_corpus_contract_digest() -> str:
    """Bind an attestation to every model contract on the production answer path."""
    material = {
        "implementation_version": REAL_CORPUS_IMPLEMENTATION_VERSION,
        "prompt_contracts": {
            operation: prompt_contract_for(operation).digest for operation in _CONTRACT_OPERATIONS
        },
    }
    return _digest(material)


def current_real_corpus_implementation_digest() -> str:
    """Bind release evidence to the Python, Rust, and TypeScript implementation tree."""
    repository = Path(__file__).resolve().parent.parent
    if not _source_implementation_tree_is_available(repository):
        return _declared_real_corpus_implementation_digest()
    return _implementation_tree_digest(repository)


def portable_artifact_digest(package_root: Path) -> str:
    """Digest immutable package payload files while excluding self-referential evidence."""
    if not package_root.is_dir():
        raise ValueError("Portable package root is unavailable.")
    inventory = []
    for path in package_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(package_root).as_posix()
        if _portable_digest_path_is_excluded(relative):
            continue
        inventory.append(
            {
                "path": relative,
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    return _digest(sorted(inventory, key=lambda item: str(item["path"])))


def portable_manifest_digest(package_root: Path) -> str:
    """Digest the immutable payload entries declared by a portable release manifest."""
    try:
        payload = json.loads((package_root / "release-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Portable release manifest is unavailable.") from error
    raw_files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(raw_files, list):
        raise ValueError("Portable release manifest file inventory is invalid.")
    inventory: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in raw_files:
        if not isinstance(value, dict):
            raise ValueError("Portable release manifest file entry is invalid.")
        relative = value.get("path")
        sha256 = value.get("sha256")
        size = value.get("bytes")
        if (
            not isinstance(relative, str)
            or not relative
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ValueError("Portable release manifest file entry is invalid.")
        normalized = relative.replace("\\", "/")
        if normalized in seen:
            raise ValueError("Portable release manifest file entry is duplicated.")
        seen.add(normalized)
        if _portable_digest_path_is_excluded(normalized):
            continue
        inventory.append({"path": normalized, "sha256": sha256, "bytes": size})
    return _digest(sorted(inventory, key=lambda item: str(item["path"])))


def _implementation_tree_digest(repository: Path) -> str:
    files = [
        path
        for root, suffixes in (
            (repository / "openkb", {".py"}),
            (repository / "frontend" / "src", {".json", ".ts", ".tsx"}),
            (repository / "desktop" / "src-tauri" / "src", {".rs"}),
        )
        for path in root.rglob("*")
        if path.is_file() and path.suffix in suffixes
    ]
    files.extend(
        path
        for path in (
            repository / "frontend" / "package.json",
            repository / "desktop" / "src-tauri" / "Cargo.toml",
        )
        if path.is_file()
    )
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(repository).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _source_implementation_tree_is_available(repository: Path) -> bool:
    return all(
        path.is_dir()
        for path in (
            repository / "openkb",
            repository / "frontend" / "src",
            repository / "desktop" / "src-tauri" / "src",
        )
    )


def _declared_real_corpus_implementation_digest() -> str:
    try:
        payload = json.loads(_IMPLEMENTATION_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "0" * 64
    if not isinstance(payload, dict):
        return "0" * 64
    if payload.get("schema_version") != "openkb.real-corpus-implementation.v1":
        return "0" * 64
    if payload.get("implementation_version") != REAL_CORPUS_IMPLEMENTATION_VERSION:
        return "0" * 64
    digest = payload.get("implementation_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return "0" * 64
    return digest


def _portable_digest_path_is_excluded(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").lstrip("/").lower()
    return normalized in _PORTABLE_DIGEST_EXCLUDED_PATHS or f"/{normalized}".endswith(
        _PORTABLE_ATTESTATION_SUFFIX
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_real_corpus_benchmark(
    path: Path = _ATTESTATION_PATH,
) -> RealCorpusBenchmarkAttestation:
    """Load the shipped aggregate-only attestation and fail closed on any drift."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return parse_real_corpus_benchmark(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return failed_real_corpus_benchmark("real_corpus_attestation_invalid")


def parse_real_corpus_benchmark(payload: object) -> RealCorpusBenchmarkAttestation:
    """Validate provenance, digest, fixed-suite coverage, and local pass thresholds."""
    if not isinstance(payload, dict):
        raise ValueError("Real-corpus benchmark attestation must be an object.")
    report_digest = _sha256(payload, "report_digest")
    unsigned = {key: value for key, value in payload.items() if key != "report_digest"}
    if _digest(unsigned) != report_digest:
        raise ValueError("Real-corpus benchmark attestation digest is invalid.")
    if _string(payload, "schema_version") != REAL_CORPUS_ATTESTATION_SCHEMA_VERSION:
        raise ValueError("Real-corpus benchmark schema is unsupported.")
    if _string(payload, "suite_id") != REAL_CORPUS_SUITE_ID:
        raise ValueError("Real-corpus benchmark suite is unsupported.")
    if _string(payload, "implementation_version") != REAL_CORPUS_IMPLEMENTATION_VERSION:
        raise ValueError("Real-corpus benchmark implementation is stale.")
    implementation_digest = _sha256(payload, "implementation_digest")
    current_implementation_digest = current_real_corpus_implementation_digest()
    if (
        current_implementation_digest != _declared_real_corpus_implementation_digest()
        or implementation_digest != current_implementation_digest
    ):
        raise ValueError("Real-corpus benchmark implementation digest is stale.")
    implementation_commit_sha = _sha1(payload, "implementation_commit_sha")
    contract_digest = _sha256(payload, "contract_digest")
    if contract_digest != current_real_corpus_contract_digest():
        raise ValueError("Real-corpus benchmark contract is stale.")
    original_baseline = _original_baseline(payload.get("original_baseline"))
    windows_acceptance = _windows_acceptance(payload.get("windows_acceptance"))
    if windows_acceptance.artifact_digest != windows_acceptance.package_manifest_digest:
        raise ValueError("Real-corpus package manifest does not describe the accepted payload.")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) < 3:
        raise ValueError("Real-corpus benchmark requires three fixed cases.")
    cases = tuple(_case_result(item) for item in raw_cases)
    if len({item.case_id for item in cases}) != len(cases):
        raise ValueError("Real-corpus benchmark case identities are duplicated.")
    sample_count = sum(item.repetitions for item in cases)
    completeness = _weighted_mean(cases, "answer_completeness")
    correctness = _weighted_mean(cases, "answer_correctness")
    citation_precision = _weighted_mean(cases, "citation_precision")
    unsupported = sum(item.unsupported_claim_count for item in cases)
    degradation_runs = sum(item.degradation_runs for item in cases)
    retrieval_replay = all(item.retrieval_replay_passed for item in cases)
    noise_rate = _rate(payload, "noise_leakage_rate")
    duplicate_rate = _rate(payload, "duplicate_identity_rate")
    multi_document_coverage = _rate(payload, "multi_document_topic_coverage")
    procedure_coverage = _rate(payload, "procedure_stage_coverage")
    automated_regression = _boolean(payload, "automated_regression_passed")
    comparison_passed = (
        completeness + _MAX_ORIGINAL_COMPLETENESS_DELTA >= original_baseline.answer_completeness
        and correctness >= original_baseline.answer_correctness
        and citation_precision >= original_baseline.citation_precision
        and degradation_runs <= original_baseline.degradation_runs
        and all(item.original_comparison_passed for item in cases)
    )
    windows_passed = (
        windows_acceptance.platform == "windows-x64"
        and windows_acceptance.artifact_kind == "windows-portable-x64"
        and windows_acceptance.questions_run >= 3
        and windows_acceptance.degradation_runs == 0
        and windows_acceptance.packaged_smoke_passed
        and windows_acceptance.cancellation_passed
        and windows_acceptance.cancellation_interruption_code == "answer_cancelled"
        and windows_acceptance.regeneration_completed
        and windows_acceptance.restart_readable
        and windows_acceptance.answer_versions_preserved
        and windows_acceptance.answer_version_count >= 2
    )
    passed = (
        sample_count >= 9
        and all(item.repetitions >= 3 for item in cases)
        and completeness >= 0.85
        and correctness >= 0.95
        and citation_precision >= 0.95
        and unsupported == 0
        and degradation_runs == 0
        and noise_rate <= 0.02
        and duplicate_rate <= 0.05
        and multi_document_coverage >= 0.85
        and procedure_coverage >= 0.85
        and retrieval_replay
        and automated_regression
        and original_baseline.sample_count >= 3
        and original_baseline.fallback_runs == 0
        and comparison_passed
        and windows_passed
    )
    if payload.get("passed") is not passed:
        raise ValueError("Real-corpus benchmark pass state is inconsistent.")
    return RealCorpusBenchmarkAttestation(
        schema_version=REAL_CORPUS_ATTESTATION_SCHEMA_VERSION,
        suite_id=REAL_CORPUS_SUITE_ID,
        implementation_version=REAL_CORPUS_IMPLEMENTATION_VERSION,
        implementation_digest=implementation_digest,
        implementation_commit_sha=implementation_commit_sha,
        corpus_digest=_sha256(payload, "corpus_digest"),
        contract_digest=contract_digest,
        model_profile_digest=_sha256(payload, "model_profile_digest"),
        report_digest=report_digest,
        collected_at=_string(payload, "collected_at"),
        source_document_count=_positive_int(payload, "source_document_count"),
        sample_count=sample_count,
        answer_completeness=completeness,
        answer_correctness=correctness,
        citation_precision=citation_precision,
        unsupported_claim_count=unsupported,
        degradation_runs=degradation_runs,
        noise_leakage_rate=noise_rate,
        duplicate_identity_rate=duplicate_rate,
        multi_document_topic_coverage=multi_document_coverage,
        procedure_stage_coverage=procedure_coverage,
        retrieval_replay_passed=retrieval_replay,
        automated_regression_passed=automated_regression,
        passed=passed,
        original_baseline=original_baseline,
        windows_acceptance=windows_acceptance,
        cases=cases,
    )


def failed_real_corpus_benchmark(code: str) -> RealCorpusBenchmarkAttestation:
    original_baseline = RealCorpusOriginalBaseline(
        commit_sha="0" * 40,
        model_profile_digest="0" * 64,
        sample_count=0,
        answer_completeness=0.0,
        answer_correctness=0.0,
        citation_precision=0.0,
        degradation_runs=0,
        fallback_runs=0,
    )
    windows_acceptance = RealCorpusWindowsAcceptance(
        platform="",
        host_os="",
        artifact_kind="",
        artifact_digest="0" * 64,
        package_manifest_digest="0" * 64,
        questions_run=0,
        degradation_runs=0,
        packaged_smoke_passed=False,
        cancellation_passed=False,
        cancellation_interruption_code="",
        regeneration_completed=False,
        restart_readable=False,
        answer_versions_preserved=False,
        answer_version_count=0,
    )
    empty = RealCorpusBenchmarkAttestation(
        schema_version=REAL_CORPUS_ATTESTATION_SCHEMA_VERSION,
        suite_id=REAL_CORPUS_SUITE_ID,
        implementation_version=REAL_CORPUS_IMPLEMENTATION_VERSION,
        implementation_digest=current_real_corpus_implementation_digest(),
        implementation_commit_sha="0" * 40,
        corpus_digest="0" * 64,
        contract_digest=current_real_corpus_contract_digest(),
        model_profile_digest="0" * 64,
        report_digest="0" * 64,
        collected_at="",
        source_document_count=0,
        sample_count=0,
        answer_completeness=0.0,
        answer_correctness=0.0,
        citation_precision=0.0,
        unsupported_claim_count=0,
        degradation_runs=0,
        noise_leakage_rate=1.0,
        duplicate_identity_rate=1.0,
        multi_document_topic_coverage=0.0,
        procedure_stage_coverage=0.0,
        retrieval_replay_passed=False,
        automated_regression_passed=False,
        passed=False,
        original_baseline=original_baseline,
        windows_acceptance=windows_acceptance,
    )
    return replace(empty, failure_code=code)


def _case_result(value: object) -> RealCorpusCaseResult:
    if not isinstance(value, dict):
        raise ValueError("Real-corpus benchmark case is invalid.")
    completeness = _rate(value, "answer_completeness")
    correctness = _rate(value, "answer_correctness")
    citation_precision = _rate(value, "citation_precision")
    degradation_runs = _nonnegative_int(value, "degradation_runs")
    original_completeness = _rate(value, "original_answer_completeness")
    original_correctness = _rate(value, "original_answer_correctness")
    original_citation_precision = _rate(value, "original_citation_precision")
    original_degradation_runs = _nonnegative_int(value, "original_degradation_runs")
    comparison_passed = (
        completeness + _MAX_ORIGINAL_COMPLETENESS_DELTA >= original_completeness
        and correctness >= original_correctness
        and citation_precision >= original_citation_precision
        and degradation_runs <= original_degradation_runs
    )
    if value.get("original_comparison_passed") is not comparison_passed:
        raise ValueError("Real-corpus original comparison state is inconsistent.")
    return RealCorpusCaseResult(
        case_id=_string(value, "case_id"),
        repetitions=_positive_int(value, "repetitions"),
        answer_completeness=completeness,
        answer_correctness=correctness,
        citation_precision=citation_precision,
        unsupported_claim_count=_nonnegative_int(value, "unsupported_claim_count"),
        degradation_runs=degradation_runs,
        retrieval_replay_passed=_boolean(value, "retrieval_replay_passed"),
        original_answer_completeness=original_completeness,
        original_answer_correctness=original_correctness,
        original_citation_precision=original_citation_precision,
        original_degradation_runs=original_degradation_runs,
        original_comparison_passed=comparison_passed,
    )


def _original_baseline(value: object) -> RealCorpusOriginalBaseline:
    if not isinstance(value, dict):
        raise ValueError("Real-corpus original baseline is invalid.")
    return RealCorpusOriginalBaseline(
        commit_sha=_sha1(value, "commit_sha"),
        model_profile_digest=_sha256(value, "model_profile_digest"),
        sample_count=_positive_int(value, "sample_count"),
        answer_completeness=_rate(value, "answer_completeness"),
        answer_correctness=_rate(value, "answer_correctness"),
        citation_precision=_rate(value, "citation_precision"),
        degradation_runs=_nonnegative_int(value, "degradation_runs"),
        fallback_runs=_nonnegative_int(value, "fallback_runs"),
    )


def _windows_acceptance(value: object) -> RealCorpusWindowsAcceptance:
    if not isinstance(value, dict):
        raise ValueError("Real-corpus Windows acceptance is invalid.")
    return RealCorpusWindowsAcceptance(
        platform=_string(value, "platform"),
        host_os=_string(value, "host_os"),
        artifact_kind=_string(value, "artifact_kind"),
        artifact_digest=_sha256(value, "artifact_digest"),
        package_manifest_digest=_sha256(value, "package_manifest_digest"),
        questions_run=_positive_int(value, "questions_run"),
        degradation_runs=_nonnegative_int(value, "degradation_runs"),
        packaged_smoke_passed=_boolean(value, "packaged_smoke_passed"),
        cancellation_passed=_boolean(value, "cancellation_passed"),
        cancellation_interruption_code=_string(value, "cancellation_interruption_code"),
        regeneration_completed=_boolean(value, "regeneration_completed"),
        restart_readable=_boolean(value, "restart_readable"),
        answer_versions_preserved=_boolean(value, "answer_versions_preserved"),
        answer_version_count=_positive_int(value, "answer_version_count"),
    )


def _weighted_mean(cases: tuple[RealCorpusCaseResult, ...], field: str) -> float:
    total = sum(item.repetitions for item in cases)
    return sum(getattr(item, field) * item.repetitions for item in cases) / total


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _string(value: dict[object, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Real-corpus benchmark {field} is invalid.")
    return result


def _sha256(value: dict[object, object], field: str) -> str:
    result = _string(value, field)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"Real-corpus benchmark {field} is invalid.")
    return result


def _sha1(value: dict[object, object], field: str) -> str:
    result = _string(value, field)
    if len(result) != 40 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"Real-corpus benchmark {field} is invalid.")
    return result


def _positive_int(value: dict[object, object], field: str) -> int:
    result = value.get(field)
    if not isinstance(result, int) or isinstance(result, bool) or result < 1:
        raise ValueError(f"Real-corpus benchmark {field} is invalid.")
    return result


def _nonnegative_int(value: dict[object, object], field: str) -> int:
    result = value.get(field)
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise ValueError(f"Real-corpus benchmark {field} is invalid.")
    return result


def _rate(value: dict[object, object], field: str) -> float:
    result = value.get(field)
    if not isinstance(result, (int, float)) or isinstance(result, bool) or not 0 <= result <= 1:
        raise ValueError(f"Real-corpus benchmark {field} is invalid.")
    return float(result)


def _boolean(value: dict[object, object], field: str) -> bool:
    result = value.get(field)
    if not isinstance(result, bool):
        raise ValueError(f"Real-corpus benchmark {field} is invalid.")
    return result
