from __future__ import annotations

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
from openkb.desktop_model_gateway import DesktopModelRequest
from openkb.desktop_prompt_contracts import prompt_contract_for

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
    assert all(
        pair.relationship == "structurally_equivalent_translation"
        for pair in definition.metamorphic_pairs
    )
    assert any(
        {pair.left.language, pair.right.language} == {"en", "zh"}
        for pair in definition.metamorphic_pairs
    )


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
        payload = json.loads(request.content)
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
    assert report["case_count"] == 7
    assert report["repetitions"] == 3
    assert report["logical_operation_count"] == 42
    assert report["physical_call_count"] == 42
    assert report["valid_operation_count"] == 42
    assert all(suite["deterministic_status"] == "passed" for suite in report["suites"])
    assert len(client.requests) == 42
    assert all(request.model_name == "deepseek-v4-flash" for request in client.requests)
    assert all(request.provider_adapter == "deepseek" for request in client.requests)
    assert all(request.provider_adapter_version == "deepseek.v2" for request in client.requests)
    assert all(request.structured_output_mode == "json_object" for request in client.requests)
    assert all(request.reasoning_effort == "off" for request in client.requests)

    output_lines = result.outputs_path.read_text(encoding="utf-8").splitlines()
    assert len(output_lines) == 42
    assert all(json.loads(line)["valid"] is True for line in output_lines)
    pending = json.loads(result.pending_attestation_path.read_text(encoding="utf-8"))
    assert pending["status"] == "pending_human_review"
    assert "output_digest" in pending["bindings"]
    assert "outputs" not in pending
    for artifact in result.run_dir.iterdir():
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
    assert report["physical_call_count"] == 84
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
    signed_path = tmp_path / "release-attestation.json"

    actual_path = sign_human_attestation(
        result.run_dir,
        review_path,
        maintainer="maintainer@example.test",
        output_path=signed_path,
    )

    assert actual_path == signed_path
    attestation = json.loads(signed_path.read_text(encoding="utf-8"))
    assert attestation["status"] == "passed"
    assert attestation["human_review"]["maintainer"] == "maintainer@example.test"
    assert all(suite["verdict"] == "pass" for suite in attestation["human_review"]["suites"])
    assert "review_digest" in attestation["human_review"]
    assert "outputs" not in attestation
    assert "evaluation-only-secret" not in signed_path.read_text(encoding="utf-8")
