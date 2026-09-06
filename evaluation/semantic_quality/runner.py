"""Release-only live evaluation for model-owned semantic structures."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from dotenv import dotenv_values

from evaluation.semantic_quality.attestation import sign_human_attestation
from evaluation.semantic_quality.definition import (
    EvaluationCase,
    EvaluationDefinition,
    LiveEvaluationProfile,
    SemanticQualityError,
    load_evaluation_definition,
)
from openkb.desktop_knowledge_page import (
    KnowledgePageClaimSnapshot,
    knowledge_page_claim_id,
)
from openkb.desktop_knowledge_page_planner import run_knowledge_page_planning
from openkb.desktop_model_contract_renderer import render_provider_visible_contract
from openkb.desktop_model_gateway import DesktopModelRequest, DesktopModelResult
from openkb.desktop_model_provider_adapter import named_provider_adapter_for
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_query_planning import QueryPlanningResult, parse_query_planning_result
from openkb.desktop_structured_output import (
    run_structured_output,
    structured_output_repair_contract_digest,
)


class EvaluationModelClient(Protocol):
    def complete(self, request: DesktopModelRequest) -> str: ...


@dataclass(frozen=True)
class EvaluationRunResult:
    run_dir: Path
    outputs_path: Path
    report_path: Path
    pending_attestation_path: Path
    status: str


class OpenAIChatCompletionClient:
    """Small non-streaming SDK boundary for the pinned evaluation profile."""

    def __init__(
        self,
        api_key: str,
        profile: LiveEvaluationProfile,
        *,
        sdk_client: object | None = None,
    ) -> None:
        if not api_key:
            raise SemanticQualityError("Live evaluation requires a non-empty API key.")
        if sdk_client is None:
            from openai import OpenAI

            sdk_client = OpenAI(
                api_key=api_key,
                base_url=profile.api_base_url,
                timeout=profile.timeout_seconds,
                max_retries=0,
            )
        self._client = sdk_client
        self._profile = profile

    def complete(self, request: DesktopModelRequest) -> str:
        """Issue one provider request using only the pinned non-secret profile controls."""
        profile = self._profile
        adapter = named_provider_adapter_for(profile.provider)
        if request.model_name != profile.model:
            raise SemanticQualityError("Evaluation request model differs from the pinned profile.")
        if request.provider_adapter != adapter.identity:
            raise SemanticQualityError(
                "Evaluation request adapter differs from the pinned profile."
            )
        if request.provider_adapter_version != adapter.version:
            raise SemanticQualityError("Evaluation request adapter version is not pinned.")
        if request.structured_output_mode != profile.structured_output_mode:
            raise SemanticQualityError("Evaluation request Structured Output Mode is not pinned.")
        if request.reasoning_effort != "off" or profile.thinking != "disabled":
            raise SemanticQualityError("Evaluation request must disable provider thinking.")
        contract = prompt_contract_for(request.operation)
        snapshot_instructions = (
            request.prompt_contract_snapshot.get("instructions")
            if request.prompt_contract_snapshot is not None
            else None
        )
        instructions = (
            snapshot_instructions
            if isinstance(snapshot_instructions, str)
            else contract.instructions
        )
        parameters = adapter.request_parameters(
            structured_output_mode=request.structured_output_mode,
            response_schema=request.response_schema,
            response_schema_name=request.response_schema_name,
            reasoning=request.reasoning_effort,
        )
        completions = getattr(getattr(self._client, "chat", None), "completions", None)
        create = getattr(completions, "create", None)
        if not callable(create):
            raise SemanticQualityError("The OpenAI-compatible SDK client is unavailable.")
        response = create(
            model=profile.model,
            messages=[
                {
                    "role": "system",
                    "content": render_provider_visible_contract(request, instructions),
                },
                {"role": "user", "content": request.content},
            ],
            temperature=profile.temperature,
            max_tokens=profile.max_output_tokens,
            **parameters,
        )
        choices = _object_value(response, "choices")
        if not isinstance(choices, list) or not choices:
            raise SemanticQualityError("The evaluation provider returned no choices.")
        content = _object_value(_object_value(choices[0], "message"), "content")
        if not isinstance(content, str) or not content.strip():
            raise SemanticQualityError("The evaluation provider returned no final content.")
        return content


def load_repository_api_key(
    repository_root: Path,
    *,
    candidate_release: bool = False,
) -> str | None:
    """Read only the repository-local ignored credential used by live evaluation."""
    env_path = repository_root.resolve() / ".env"
    try:
        api_key = dotenv_values(env_path).get("LLM_API_KEY") if env_path.is_file() else None
    except OSError as error:
        raise SemanticQualityError("Cannot read the repository-local .env file.") from error
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    if candidate_release:
        raise SemanticQualityError(
            "LLM_API_KEY is required in the repository-local .env for candidate evaluation."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the two-stage release gate without ever printing credentials or raw outputs."""
    parser = argparse.ArgumentParser(description="OpenKB live semantic quality gate")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run the pinned live-model matrix")
    run_parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    run_parser.add_argument("--output-root", type=Path)
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--candidate-release", action="store_true")
    sign_parser = subparsers.add_parser("sign", help="bind a human-authored rubric review")
    sign_parser.add_argument("run_dir", type=Path)
    sign_parser.add_argument("review", type=Path)
    sign_parser.add_argument("--maintainer", required=True)
    sign_parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            result = run_live_evaluation(
                arguments.repository_root,
                output_root=arguments.output_root,
                run_id=arguments.run_id,
                candidate_release=arguments.candidate_release,
            )
            if result is None:
                print(
                    "SKIPPED: repository-local .env has no LLM_API_KEY; "
                    "deterministic tests remain available."
                )
                return 0
            print(f"{result.status}: {result.report_path}")
            return 0 if result.status == "pending_human_review" else 1
        signed_path = sign_human_attestation(
            arguments.run_dir,
            arguments.review,
            maintainer=arguments.maintainer,
            output_path=arguments.output,
        )
        attestation = _artifact_mapping(signed_path, "signed semantic attestation")
        print(f"{attestation['status']}: {signed_path}")
        return 0 if attestation.get("status") == "passed" else 1
    except SemanticQualityError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


def run_live_evaluation(
    repository_root: Path,
    *,
    client: EvaluationModelClient | None = None,
    output_root: Path | None = None,
    run_id: str | None = None,
    candidate_release: bool = False,
) -> EvaluationRunResult | None:
    """Run all pinned cases and write raw local outputs plus a digest-only pending record."""
    repository_root = repository_root.resolve()
    definition = load_evaluation_definition(repository_root)
    if client is None:
        api_key = load_repository_api_key(
            repository_root,
            candidate_release=candidate_release,
        )
        if api_key is None:
            return None
        client = OpenAIChatCompletionClient(api_key, definition.profile)
    run_id = run_id or _new_run_id()
    _validate_run_id(run_id)
    run_dir = (output_root or repository_root / ".semantic-eval") / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise SemanticQualityError(
            "Cannot create a new semantic evaluation run directory."
        ) from error

    records: list[dict[str, object]] = []
    for repetition in range(1, definition.profile.repetitions + 1):
        for case in definition.cases:
            records.append(
                _execute_query_planning(
                    case,
                    repetition=repetition,
                    profile=definition.profile,
                    client=client,
                )
            )
            records.append(
                _execute_knowledge_page_planning(
                    case,
                    repetition=repetition,
                    profile=definition.profile,
                    client=client,
                )
            )

    outputs_path = run_dir / "outputs.jsonl"
    output_bytes = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")
    try:
        outputs_path.write_bytes(output_bytes)
    except OSError as error:
        raise SemanticQualityError("Cannot write semantic evaluation outputs.") from error
    output_digest = hashlib.sha256(output_bytes).hexdigest()
    valid_count = sum(record["valid"] is True for record in records)
    physical_calls = sum(len(record["attempts"]) for record in records)
    status = "pending_human_review" if valid_count == len(records) else "deterministic_failed"
    bindings = _evaluation_bindings(
        repository_root,
        definition,
        output_digest=output_digest,
    )
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report: dict[str, object] = {
        "schema_version": "openkb.semantic-quality-report.v1",
        "run_id": run_id,
        "created_at": created_at,
        "candidate_release": candidate_release,
        "status": status,
        "case_count": len(definition.cases),
        "repetitions": definition.profile.repetitions,
        "logical_operation_count": len(records),
        "physical_call_count": physical_calls,
        "valid_operation_count": valid_count,
        "invalid_operation_count": len(records) - valid_count,
        "suites": _suite_results(definition, records),
        "metamorphic_pairs": [
            {
                "pair_id": pair.pair_id,
                "left_suite_id": pair.left.suite_id,
                "right_suite_id": pair.right.suite_id,
                "relationship": pair.relationship,
                "human_review_required": True,
            }
            for pair in definition.metamorphic_pairs
        ],
        "bindings": bindings,
        "human_review_required": True,
    }
    report_path = run_dir / "report.json"
    _write_json(report_path, report)
    pending = {
        "schema_version": "openkb.semantic-quality-attestation.v1",
        "run_id": run_id,
        "created_at": created_at,
        "status": status,
        "bindings": bindings,
        "deterministic": {
            "valid_operation_count": valid_count,
            "logical_operation_count": len(records),
        },
        "human_review": {
            "required": True,
            "status": "pending",
            "suite_dimensions": list(definition.suite_dimensions),
            "pair_dimensions": list(definition.pair_dimensions),
        },
    }
    pending_path = run_dir / "attestation.pending.json"
    _write_json(pending_path, pending)
    return EvaluationRunResult(run_dir, outputs_path, report_path, pending_path, status)


def _execute_query_planning(
    case: EvaluationCase,
    *,
    repetition: int,
    profile: LiveEvaluationProfile,
    client: EvaluationModelClient,
) -> dict[str, object]:
    source_material = json.dumps(
        {
            "schema_version": "openkb.query-planning-input.v1",
            "question": case.question,
            "conversation_context": [],
            "seed_observations": [
                {
                    "evidence_id": item.evidence_id,
                    "document_name": item.document_name,
                    "section": item.section,
                    "excerpt": item.excerpt,
                }
                for item in case.evidence
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    attempts: list[dict[str, object]] = []
    invoke = _evaluation_invoker(
        case,
        repetition=repetition,
        profile=profile,
        client=client,
        attempts=attempts,
    )

    def validate(content: str) -> QueryPlanningResult:
        value = parse_query_planning_result(
            content,
            question=case.question,
            conversation_context_digest=_canonical_digest([]),
            seed_evidence_ids=frozenset(item.evidence_id for item in case.evidence),
        )
        if value.retrieval_plan is None or value.semantic_structure_state != "known":
            raise ValueError("query_planning_deterministic_invariant_failed")
        return value

    try:
        output = run_structured_output(
            operation="query_planning",
            document_name=f"semantic-evaluation:{case.case_id}",
            source_material=source_material,
            invoke=invoke,
            validate=validate,
            repair_output_limit=True,
        )
        result = _query_result_payload(output.value)
        return _operation_record(
            case,
            repetition=repetition,
            operation="query_planning",
            valid=True,
            repaired=output.repaired,
            attempts=attempts,
            validated_result=result,
        )
    except Exception as error:
        return _operation_record(
            case,
            repetition=repetition,
            operation="query_planning",
            valid=False,
            repaired=len(attempts) > 1,
            attempts=attempts,
            failure_kind=type(error).__name__,
        )


def _execute_knowledge_page_planning(
    case: EvaluationCase,
    *,
    repetition: int,
    profile: LiveEvaluationProfile,
    client: EvaluationModelClient,
) -> dict[str, object]:
    generation_id = repetition
    claims = tuple(
        KnowledgePageClaimSnapshot(
            generation_id=generation_id,
            identity_id=case.page.identity_id,
            candidate_generation_id=f"eval-{case.case_id}-{repetition}",
            candidate_id=f"eval-{case.case_id}",
            claim_ordinal=ordinal,
            claim_id=knowledge_page_claim_id(
                generation_id,
                case.page.identity_id,
                claim.text,
                claim.applicability,
            ),
            text=claim.text,
            applicability=claim.applicability,
            evidence_ids=claim.evidence_ids,
        )
        for ordinal, claim in enumerate(case.page.claims)
    )
    attempts: list[dict[str, object]] = []
    invoke = _evaluation_invoker(
        case,
        repetition=repetition,
        profile=profile,
        client=client,
        attempts=attempts,
    )
    try:
        planning = run_knowledge_page_planning(
            document_name=f"semantic-evaluation:{case.case_id}",
            generation_id=generation_id,
            identity_id=case.page.identity_id,
            title=case.page.title,
            claims=claims,
            relations=(),
            knowledge_language=case.language,
            invoke=invoke,
        )
        return _operation_record(
            case,
            repetition=repetition,
            operation="knowledge_page_planning",
            valid=True,
            repaired=planning.repaired,
            attempts=attempts,
            validated_result={
                "plan_digest": planning.plan.digest,
                "placed_claim_ids": list(planning.plan.placed_claim_ids),
            },
        )
    except Exception as error:
        return _operation_record(
            case,
            repetition=repetition,
            operation="knowledge_page_planning",
            valid=False,
            repaired=len(attempts) > 1,
            attempts=attempts,
            failure_kind=type(error).__name__,
        )


def _evaluation_invoker(
    case: EvaluationCase,
    *,
    repetition: int,
    profile: LiveEvaluationProfile,
    client: EvaluationModelClient,
    attempts: list[dict[str, object]],
):
    adapter = named_provider_adapter_for(profile.provider)

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        pinned = replace(
            request,
            model_role="analysis",
            model_name=profile.model,
            reasoning_effort="off",
            provider_adapter=adapter.identity,
            provider_adapter_version=adapter.version,
            structured_output_mode=profile.structured_output_mode,
            response_timeout_seconds=profile.timeout_seconds,
        )
        attempt: dict[str, object] = {"operation": pinned.operation, "output": None}
        attempts.append(attempt)
        content = client.complete(pinned)
        attempt["output"] = content
        return DesktopModelResult(
            call_id=(
                f"semantic-eval-{case.case_id}-{repetition}-{pinned.operation}-{len(attempts)}"
            ),
            content=content,
            attempt_count=1,
        )

    return invoke


def _operation_record(
    case: EvaluationCase,
    *,
    repetition: int,
    operation: str,
    valid: bool,
    repaired: bool,
    attempts: list[dict[str, object]],
    validated_result: dict[str, object] | None = None,
    failure_kind: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": "openkb.semantic-quality-output.v1",
        "suite_id": case.suite_id,
        "case_id": case.case_id,
        "domain": case.domain,
        "language": case.language,
        "repetition": repetition,
        "operation": operation,
        "valid": valid,
        "repaired": repaired,
        "attempts": attempts,
    }
    if validated_result is not None:
        record["validated_result"] = validated_result
    if failure_kind is not None:
        record["failure_kind"] = failure_kind
    return record


def _query_result_payload(result: QueryPlanningResult) -> dict[str, object]:
    assert result.retrieval_plan is not None
    assert result.facet_plan is not None
    return {
        "retrieval_terms": list(result.retrieval_plan.terms),
        "goal": result.facet_plan.goal,
        "facets": [
            {
                "facet_id": facet.facet_id,
                "label": facet.label,
                "description": facet.description,
                "importance": facet.importance,
            }
            for facet in result.facet_plan.facets
        ],
        "coverage": [
            {
                "facet_id": entry.facet_id,
                "state": entry.state,
                "evidence_ids": list(entry.evidence_ids),
            }
            for entry in result.coverage
        ],
    }


def _suite_results(
    definition: EvaluationDefinition,
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for case in definition.cases:
        selected = [record for record in records if record["case_id"] == case.case_id]
        valid_count = sum(record["valid"] is True for record in selected)
        results.append(
            {
                "suite_id": case.suite_id,
                "case_id": case.case_id,
                "domain": case.domain,
                "language": case.language,
                "deterministic_status": ("passed" if valid_count == len(selected) else "failed"),
                "valid_operation_count": valid_count,
                "logical_operation_count": len(selected),
                "failure_kinds": sorted(
                    {str(record["failure_kind"]) for record in selected if "failure_kind" in record}
                ),
            }
        )
    return results


def _evaluation_bindings(
    repository_root: Path,
    definition: EvaluationDefinition,
    *,
    output_digest: str,
) -> dict[str, object]:
    return {
        "profile_digest": definition.profile_digest,
        "matrix_digest": definition.matrix_digest,
        "rubric_digest": definition.rubric_digest,
        "output_digest": output_digest,
        "implementation_digest": _implementation_digest(repository_root),
        "prompt_contract_digests": {
            "query_planning": prompt_contract_for("query_planning").digest,
            "query_planning_repair": structured_output_repair_contract_digest("query_planning"),
            "knowledge_page_planning": prompt_contract_for("knowledge_page_planning").digest,
            "knowledge_page_planning_repair": structured_output_repair_contract_digest(
                "knowledge_page_planning"
            ),
        },
        "profile": {
            "provider": definition.profile.provider,
            "api_base_url": definition.profile.api_base_url,
            "model": definition.profile.model,
            "structured_output_mode": definition.profile.structured_output_mode,
            "thinking": definition.profile.thinking,
            "repetitions": definition.profile.repetitions,
        },
    }


def _implementation_digest(repository_root: Path) -> str:
    relative_paths = (
        "evaluation/semantic_quality/attestation.py",
        "evaluation/semantic_quality/definition.py",
        "evaluation/semantic_quality/runner.py",
        "openkb/desktop_knowledge_page.py",
        "openkb/desktop_knowledge_page_planner.py",
        "openkb/desktop_knowledge_page_planning.py",
        "openkb/desktop_model_contract_renderer.py",
        "openkb/desktop_model_provider_adapter.py",
        "openkb/desktop_prompt_contracts.py",
        "openkb/desktop_query_planning.py",
        "openkb/desktop_semantic_structure_contracts.py",
        "openkb/desktop_structured_output.py",
    )
    file_digests: dict[str, str] = {}
    try:
        for relative_path in relative_paths:
            file_digests[relative_path] = hashlib.sha256(
                (repository_root / relative_path).read_bytes()
            ).hexdigest()
    except OSError as error:
        raise SemanticQualityError("Cannot bind the semantic evaluation implementation.") from error
    return _canonical_digest(file_digests)


def _write_json(path: Path, value: object) -> None:
    try:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise SemanticQualityError(f"Cannot write evaluation artifact: {path.name}") from error


def _artifact_mapping(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise SemanticQualityError(f"Cannot read the {name}.") from error
    if not isinstance(value, dict):
        raise SemanticQualityError(f"The {name} must be a JSON object.")
    return value


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _validate_run_id(run_id: str) -> None:
    if (
        not run_id
        or len(run_id) > 120
        or any(not (character.isalnum() or character in "-_.") for character in run_id)
    ):
        raise SemanticQualityError("The semantic evaluation run ID is not a safe path segment.")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _object_value(value: object, field: str) -> object:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)
