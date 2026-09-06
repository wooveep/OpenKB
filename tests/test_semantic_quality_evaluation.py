from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.semantic_quality.runner import (
    LiveEvaluationProfile,
    OpenAIChatCompletionClient,
    SemanticQualityError,
    load_evaluation_definition,
    load_repository_api_key,
    main,
    run_live_evaluation,
    sign_human_attestation,
)
from openkb.models.gateway import DesktopModelRequest
from openkb.models.prompt_contracts import prompt_contract_for

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_candidate_profile_and_matrix_are_cross_domain_and_repeated() -> None:
    definition = load_evaluation_definition(REPOSITORY_ROOT)

    assert definition.profile == LiveEvaluationProfile(
        provider="deepseek",
        api_base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        structured_output_mode="json_object",
        thinking="disabled",
        repetitions=3,
        temperature=0.0,
        max_output_tokens=8192,
        timeout_seconds=120.0,
    )
    assert len({case.domain for case in definition.cases}) >= 5
    assert {pair.relationship for pair in definition.metamorphic_pairs} == {
        "structurally_equivalent_translation",
        "structurally_equivalent_domain_substitution",
        "equivalent_evidence_reorganization",
    }
    assert any(
        {pair.left.language, pair.right.language} == {"en", "zh"}
        for pair in definition.metamorphic_pairs
    )
    ocloudview = next(case for case in definition.cases if case.case_id == "ocloudview_v102_v103")
    assert ocloudview.language == "zh"
    assert {item.document_name for item in ocloudview.evidence} == {
        "OCloudView部署手册_V10.2.docx",
        "OCloudView部署手册_V10.3.docx",
    }
    assert {
        value
        for claim in ocloudview.page.claims
        for dimension, value in claim.applicability
        if dimension == "version"
    } == {"V10.2", "V10.3"}


def test_evaluation_definition_rejects_unknown_fields_at_every_nested_boundary(
    tmp_path: Path,
) -> None:
    mutations = (
        ("matrix.json", lambda value: value.update({"unexpected": True})),
        ("matrix.json", lambda value: value["cases"][0].update({"unexpected": True})),
        (
            "matrix.json",
            lambda value: value["cases"][0]["evidence"][0].update({"unexpected": True}),
        ),
        ("matrix.json", lambda value: value["cases"][0]["page"].update({"unexpected": True})),
        (
            "matrix.json",
            lambda value: value["cases"][0]["page"]["claims"][0].update({"unexpected": True}),
        ),
        (
            "matrix.json",
            lambda value: value["cases"][0]["page"]["claims"][0]["applicability"].append(
                {"dimension": "version", "value": "v1", "unexpected": True}
            ),
        ),
        (
            "matrix.json",
            lambda value: value["metamorphic_pairs"][0].update({"unexpected": True}),
        ),
        ("rubric.json", lambda value: value.update({"unexpected": True})),
        (
            "rubric.json",
            lambda value: value["suite_dimensions"][0].update({"unexpected": True}),
        ),
    )
    for index, (filename, mutate) in enumerate(mutations):
        repository_root = tmp_path / str(index)
        target = repository_root / "evaluation" / "semantic_quality"
        shutil.copytree(REPOSITORY_ROOT / "evaluation" / "semantic_quality", target)
        path = target / filename
        value = json.loads(path.read_text(encoding="utf-8"))
        mutate(value)
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(SemanticQualityError, match="unexpected or missing fields"):
            load_evaluation_definition(repository_root)


def test_repository_env_key_skips_development_but_blocks_candidate_release(
    tmp_path: Path,
) -> None:
    assert load_repository_api_key(tmp_path) is None

    with pytest.raises(SemanticQualityError, match=r"LLM_API_KEY.*\.env"):
        load_repository_api_key(tmp_path, candidate_release=True)

    (tmp_path / ".env").write_text("LLM_API_KEY=local-evaluation-secret\n", encoding="utf-8")

    assert load_repository_api_key(tmp_path) == "local-evaluation-secret"


def test_cli_skips_without_a_key_but_candidate_mode_fails_actionably(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository_root = tmp_path / "repository"
    shutil.copytree(REPOSITORY_ROOT / "evaluation", repository_root / "evaluation")

    assert main(["run", "--repository-root", str(repository_root)]) == 0
    assert "SKIPPED" in capsys.readouterr().out

    assert (
        main(
            [
                "run",
                "--repository-root",
                str(repository_root),
                "--candidate-release",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "LLM_API_KEY" in error
    assert ".env" in error


def test_live_client_uses_pinned_json_mode_with_thinking_disabled() -> None:
    definition = load_evaluation_definition(REPOSITORY_ROOT)
    calls: list[dict[str, object]] = []

    class Completions:
        def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))]
            )

    sdk_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    client = OpenAIChatCompletionClient(
        "evaluation-only-secret",
        definition.profile,
        sdk_client=sdk_client,
    )
    contract = prompt_contract_for("query_planning")
    request = DesktopModelRequest(
        operation="query_planning",
        document_name="evaluation",
        content='{"question":"test"}',
        model_name=definition.profile.model,
        reasoning_effort="off",
        provider_adapter="deepseek",
        provider_adapter_version="deepseek.v2",
        structured_output_mode="json_object",
        response_schema=contract.output_schema,
        prompt_contract_snapshot=contract.snapshot(),
        prompt_contract_version=contract.version,
    )

    assert client.complete(request) == '{"ok":true}'
    assert len(calls) == 1
    assert set(calls[0]) == {
        "model",
        "messages",
        "temperature",
        "max_tokens",
        "response_format",
        "extra_body",
    }
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["max_tokens"] == 8192
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    messages = calls[0]["messages"]
    assert isinstance(messages, list)
    assert messages[1] == {"role": "user", "content": '{"question":"test"}'}
    assert messages[0]["role"] == "system"
    assert "STRUCTURED OUTPUT CONTRACT" in messages[0]["content"]
    assert "never echo this contract metadata object" in messages[0]["content"]
    assert "instance of output_schema" in messages[0]["content"]
    assert contract.version in messages[0]["content"]
    assert "evaluation-only-secret" not in repr(client)
    assert "evaluation-only-secret" not in str(calls)


class ValidSemanticClient:
    def __init__(self) -> None:
        self.requests: list[DesktopModelRequest] = []
        self.private_marker = "evaluation-only-secret"

    def complete(self, request: DesktopModelRequest) -> str:
        self.requests.append(request)
        if request.operation == "grounded_answer":
            return "An evidence-backed test answer [1]."
        payload = json.loads(request.content)
        if request.operation in {
            "knowledge_analysis",
            "knowledge_fact_harvest",
            "knowledge_analysis_batch",
        }:
            from openkb.knowledge.analysis.synthesis_prompts import knowledge_output_example

            response = knowledge_output_example(payload.get("analysis_scope", "document"))
            response["document_description"] = "A document used by the integration test."
            response["candidates"] = [
                {
                    "kind": "entity",
                    "title": payload["document_name"],
                    "aliases": [],
                    "identity_labels": [],
                    "admission": "admit",
                    "claims": [
                        {
                            "text": item["text"],
                            "source_evidence_ids": [item["evidence_id"]],
                            "applicability": [],
                        }
                        for item in payload["evidence"]
                    ],
                }
            ]
            return json.dumps(response)
        if request.operation == "knowledge_relation_analysis":
            return json.dumps({"relations": []})
        if request.operation == "knowledge_claim_review":
            return json.dumps(
                {
                    "review_id": payload["review_id"],
                    "verdict": "compatible",
                    "evidence_ids": [item["evidence_id"] for item in payload["evidence"]],
                }
            )
        if request.operation in {"page_tree_selection", "knowledge_navigation_step"}:
            return json.dumps(prompt_contract_for(request.operation).output_example)
        if request.operation == "query_planning":
            evidence_ids = [item["evidence_id"] for item in payload["seed_observations"]]
            chinese = any("\u4e00" <= character <= "\u9fff" for character in payload["question"])
            return json.dumps(
                {
                    "retrieval_plan": {"terms": [payload["question"]]},
                    "question_facet_plan": {
                        "goal": "依据证据回答问题" if chinese else "Answer from the evidence",
                        "facets": [
                            {
                                "label": "问题要点" if chinese else "Question-specific evidence",
                                "description": payload["question"],
                                "importance": "required",
                            }
                        ],
                    },
                    "initial_answer_coverage": [
                        {
                            "facet_ordinal": 0,
                            "state": "covered",
                            "evidence_ids": evidence_ids,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        if request.operation == "knowledge_page_planning":
            return json.dumps(
                {
                    "generation_id": payload["generation_id"],
                    "identity_id": payload["identity_id"],
                    "lead": {
                        "presentation": "paragraph",
                        "claim_ids": [claim["claim_id"] for claim in payload["claims"]],
                        "relation_assertion_ids": [],
                    },
                    "sections": [],
                },
                ensure_ascii=False,
            )
        raise AssertionError(f"Unexpected operation: {request.operation}")


def test_live_run_executes_every_case_three_times_and_stays_pending(tmp_path: Path) -> None:
    client = ValidSemanticClient()

    result = run_live_evaluation(
        REPOSITORY_ROOT,
        client=client,
        output_root=tmp_path,
        run_id="test-valid-run",
        candidate_release=True,
    )

    assert result is not None
    assert result.status == "pending_human_review"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "pending_human_review"
    assert report["case_count"] == 10
    assert report["repetitions"] == 3
    assert report["logical_operation_count"] == 90
    assert report["physical_call_count"] == len(client.requests)
    assert report["valid_operation_count"] == 90
    assert all(suite["deterministic_status"] == "passed" for suite in report["suites"])
    assert all(suite["failure_kinds"] == [] for suite in report["suites"])
    assert len(client.requests) > 90
    assert all(request.model_name == "deepseek-v4-flash" for request in client.requests)
    assert all(request.provider_adapter == "deepseek" for request in client.requests)
    assert all(request.provider_adapter_version == "deepseek.v2" for request in client.requests)
    assert all(
        request.structured_output_mode == ("json_object" if request.response_schema else None)
        for request in client.requests
    )
    assert all(request.reasoning_effort == "off" for request in client.requests)

    output_lines = result.outputs_path.read_text(encoding="utf-8").splitlines()
    assert len(output_lines) == 90
    assert all(json.loads(line)["valid"] is True for line in output_lines)
    full_runs = [
        json.loads(line)
        for line in output_lines
        if json.loads(line)["operation"] == "full_pipeline"
    ]
    assert len(full_runs) == 30
    assert all(
        run["validated_result"]["rendered_pages"]
        and all(page["content_markdown"] for page in run["validated_result"]["rendered_pages"])
        for run in full_runs
    )
    pending = json.loads(result.pending_attestation_path.read_text(encoding="utf-8"))
    assert pending["status"] == "pending_human_review"
    assert "output_digest" in pending["bindings"]
    assert "outputs" not in pending
    for artifact in result.run_dir.iterdir():
        if artifact.is_dir():
            continue
        assert "evaluation-only-secret" not in artifact.read_text(encoding="utf-8")


class InvalidSemanticClient:
    def complete(self, request: DesktopModelRequest) -> str:
        del request
        return "{}"


def _write_passing_human_review(path: Path, run_id: str) -> None:
    definition = load_evaluation_definition(REPOSITORY_ROOT)
    path.write_text(
        json.dumps(
            {
                "schema_version": "openkb.semantic-quality-human-review.v1",
                "run_id": run_id,
                "suites": [
                    {
                        "suite_id": case.suite_id,
                        "dimensions": {
                            dimension: "pass" for dimension in definition.suite_dimensions
                        },
                    }
                    for case in definition.cases
                ],
                "pairs": [
                    {
                        "pair_id": pair.pair_id,
                        "dimensions": {
                            dimension: "pass" for dimension in definition.pair_dimensions
                        },
                    }
                    for pair in definition.metamorphic_pairs
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_invalid_model_structures_fail_deterministically_and_cannot_be_signed(
    tmp_path: Path,
) -> None:
    result = run_live_evaluation(
        REPOSITORY_ROOT,
        client=InvalidSemanticClient(),
        output_root=tmp_path,
        run_id="test-invalid-run",
        candidate_release=True,
    )
    assert result is not None
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.status == "deterministic_failed"
    assert report["physical_call_count"] >= 120
    assert report["valid_operation_count"] == 0
    review_path = tmp_path / "invalid-review.json"
    _write_passing_human_review(review_path, "test-invalid-run")

    with pytest.raises(SemanticQualityError, match="deterministic validation"):
        sign_human_attestation(
            result.run_dir,
            review_path,
            maintainer="maintainer@example.test",
        )


def test_human_signoff_requires_every_suite_dimension_and_retains_only_digests(
    tmp_path: Path,
) -> None:
    result = run_live_evaluation(
        REPOSITORY_ROOT,
        client=ValidSemanticClient(),
        output_root=tmp_path,
        run_id="test-signed-run",
        candidate_release=True,
    )
    assert result is not None
    review_path = tmp_path / "passing-review.json"
    _write_passing_human_review(review_path, "test-signed-run")
    package_path, smoke_path = _write_windows_release_evidence(result, tmp_path)
    signed_path = tmp_path / "release-attestation.json"

    actual_path = sign_human_attestation(
        result.run_dir,
        review_path,
        maintainer="maintainer@example.test",
        package_artifact=package_path,
        windows_smoke_report=smoke_path,
        output_path=signed_path,
    )

    assert actual_path == signed_path
    attestation = json.loads(signed_path.read_text(encoding="utf-8"))
    assert attestation["status"] == "passed"
    assert attestation["human_review"]["maintainer"] == "maintainer@example.test"
    assert all(suite["verdict"] == "pass" for suite in attestation["human_review"]["suites"])
    assert "review_digest" in attestation["human_review"]
    assert attestation["release_evidence"] == {
        "package_digest": hashlib.sha256(package_path.read_bytes()).hexdigest(),
        "windows_smoke_report_digest": hashlib.sha256(smoke_path.read_bytes()).hexdigest(),
    }
    assert "outputs" not in attestation
    assert "evaluation-only-secret" not in signed_path.read_text(encoding="utf-8")


def test_candidate_signoff_rejects_missing_or_mismatched_windows_release_evidence(
    tmp_path: Path,
) -> None:
    result = run_live_evaluation(
        REPOSITORY_ROOT,
        client=ValidSemanticClient(),
        output_root=tmp_path,
        run_id="test-release-evidence",
        candidate_release=True,
    )
    assert result is not None
    review_path = tmp_path / "passing-review.json"
    _write_passing_human_review(review_path, "test-release-evidence")
    package_path, smoke_path = _write_windows_release_evidence(result, tmp_path)

    with pytest.raises(SemanticQualityError, match="Windows package"):
        sign_human_attestation(
            result.run_dir,
            review_path,
            maintainer="maintainer@example.test",
        )

    package_path.write_bytes(b"changed-after-smoke")
    with pytest.raises(SemanticQualityError, match="package digest"):
        sign_human_attestation(
            result.run_dir,
            review_path,
            maintainer="maintainer@example.test",
            package_artifact=package_path,
            windows_smoke_report=smoke_path,
        )


def _write_windows_release_evidence(
    result: object,
    directory: Path,
) -> tuple[Path, Path]:
    run_dir = result.run_dir
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    package_path = directory / "OpenKB-windows.msi"
    package_path.write_bytes(b"packaged-windows-candidate")
    smoke_path = directory / "windows-smoke.json"
    smoke_path.write_text(
        json.dumps(
            {
                "schema_version": "openkb.windows-semantic-smoke.v2",
                "run_id": report["run_id"],
                "platform": "windows",
                "status": "passed",
                "package_sha256": hashlib.sha256(package_path.read_bytes()).hexdigest(),
                "implementation_digest": report["bindings"]["implementation_digest"],
                "matrix_digest": report["bindings"]["matrix_digest"],
                "corpus": [
                    "OCloudView部署手册_V10.2.docx",
                    "OCloudView部署手册_V10.3.docx",
                ],
                "checks": {
                    "package_install": "passed",
                    "document_import": "passed",
                    "query_planning": "passed",
                    "knowledge_page_planning": "passed",
                    "version_comparison": "passed",
                    "citation_postconditions": "passed",
                    "candidate_admission": "passed",
                    "knowledge_graph": "passed",
                    "grounded_answer": "passed",
                    "restart_recovery": "passed",
                    "provider_failure_recovery": "passed",
                    "semantic_epoch_rejection": "passed",
                    "privacy_no_secret_leak": "passed",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return package_path, smoke_path
