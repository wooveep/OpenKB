"""Revision-bound authority for Grounded Answer structured retrieval."""

from __future__ import annotations

import json
import sqlite3

from openkb import desktop_model_transport, desktop_page_tree_selection, desktop_retrieval_planning
from openkb.desktop_grounded_answer import DesktopGroundedAnswerService
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_operation_state import DesktopModelOperationContractStore
from openkb.desktop_model_result_failure import authorize_model_operation_retry
from openkb.desktop_model_settings import save_desktop_model_settings
from openkb.desktop_page_tree_selection import select_page_tree_evidence
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_planning import build_query_plan
from openkb.desktop_structured_output import structured_output_repair_contract_digest
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def _knowledge_base(tmp_path):
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "routing.md"
    source.write_text(
        "# Alpha\n\nAlpha detail facts remain in original evidence.\n\n"
        "## Beta\n\nBeta relates to Alpha through the routing layer.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    return kb_dir


def _gateway(kb_dir, monkeypatch, transport_type):
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )
    monkeypatch.setattr(
        desktop_model_transport,
        "DesktopLiteLLMTransport",
        transport_type,
    )
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None
    DesktopModelCapabilityStore(kb_dir).mark_verified(
        gateway.execution_profile_for_operation("query_planning")
    )
    return gateway


def _contracts(operation: str) -> tuple[tuple[str, str | None], ...]:
    return (
        (operation, prompt_contract_for(operation).digest),
        ("structured_output_repair", structured_output_repair_contract_digest(operation)),
    )


def _authorize_contracts(kb_dir, gateway, retry_scope: str, operation: str) -> None:
    for contract_operation, prompt_contract_digest in _contracts(operation):
        authorize_model_operation_retry(
            kb_dir,
            gateway,
            operation=contract_operation,
            retry_scope=retry_scope,
            prompt_contract_digest=prompt_contract_digest,
        )


def _suspend(kb_dir, gateway, operation: str, reason: str) -> None:
    profile = gateway.execution_profile_for_operation(operation)
    DesktopModelOperationContractStore(kb_dir).suspend(
        operation=operation,
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for(operation).digest,
        failure_code="model_response_invalid",
        reason=reason,
        failure_stage="domain_validation",
    )


def _state(kb_dir, gateway, operation: str):
    profile = gateway.execution_profile_for_operation(operation)
    return DesktopModelOperationContractStore(kb_dir).state(
        operation=operation,
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=prompt_contract_for(operation).digest,
    )


def _selection_response(request) -> str:
    prompt = json.loads(request.content)
    tree = prompt["trees"][0]
    node = next(item for item in tree["nodes"] if item["depth"] > 0)
    return json.dumps(
        {"selections": [{"document_id": tree["document_id"], "node_ids": [node["node_id"]]}]}
    )


def _query_planning_response() -> str:
    return json.dumps(
        {
            "retrieval_plan": {"terms": ["alpha"]},
            "question_facet_plan": {
                "goal": "Answer the question from available Evidence.",
                "facets": [
                    {
                        "label": "Available Evidence",
                        "description": "What the available Evidence establishes.",
                        "importance": "required",
                    }
                ],
            },
            "initial_answer_coverage": [
                {"facet_ordinal": 0, "state": "missing", "evidence_ids": []}
            ],
        }
    )


def test_retrieval_retry_cannot_authorize_a_suspension_created_after_precheck(
    tmp_path, monkeypatch
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    provider_calls: list[str] = []

    class ValidTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            provider_calls.append(request.operation)
            return _query_planning_response()

    gateway = _gateway(kb_dir, monkeypatch, ValidTransport)
    retry_scope = "grounded-answer:precheck-race"
    _authorize_contracts(kb_dir, gateway, retry_scope, "query_planning")
    original = desktop_retrieval_planning.model_operation_dispatch_possible
    injected = False

    def suspend_after_precheck(*args, **kwargs):
        nonlocal injected
        allowed = original(*args, **kwargs)
        if not injected:
            injected = True
            _suspend(kb_dir, gateway, "query_planning", "newer pre-dispatch suspension")
        return allowed

    monkeypatch.setattr(
        desktop_retrieval_planning,
        "model_operation_dispatch_possible",
        suspend_after_precheck,
    )

    result = build_query_plan(
        "What is Alpha?",
        gateway,
        kb_dir=kb_dir,
        retry_scope=retry_scope,
    )

    assert result.degradations == ("query_planning_suspended",)
    assert provider_calls == []
    assert _state(kb_dir, gateway, "query_planning").status == "suspended"


def test_retrieval_retry_success_does_not_clear_a_newer_suspension(tmp_path, monkeypatch) -> None:
    kb_dir = _knowledge_base(tmp_path)

    class SuspendDuringTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, _request, _timeout_seconds):
            _suspend(kb_dir, gateway, "query_planning", "newer in-flight suspension")
            return _query_planning_response()

    gateway = _gateway(kb_dir, monkeypatch, SuspendDuringTransport)
    _suspend(kb_dir, gateway, "query_planning", "observed suspension")
    retry_scope = "grounded-answer:in-flight-race"
    _authorize_contracts(kb_dir, gateway, retry_scope, "query_planning")

    result = build_query_plan(
        "What is Alpha?",
        gateway,
        kb_dir=kb_dir,
        retry_scope=retry_scope,
    )

    assert result.degradations == ()
    state = _state(kb_dir, gateway, "query_planning")
    assert state.status == "suspended"
    assert state.reason == "newer in-flight suspension"


def test_page_selection_retry_cannot_authorize_a_suspension_created_after_precheck(
    tmp_path, monkeypatch
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    provider_calls: list[str] = []

    class ValidTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            provider_calls.append(request.operation)
            return _selection_response(request)

    gateway = _gateway(kb_dir, monkeypatch, ValidTransport)
    baseline = DesktopEvidenceRetriever(kb_dir).retrieve("Compare Alpha and Beta")
    retry_scope = "grounded-answer:selection-precheck-race"
    _authorize_contracts(kb_dir, gateway, retry_scope, "page_tree_selection")
    original = desktop_page_tree_selection.model_operation_dispatch_possible
    injected = False

    def suspend_after_precheck(*args, **kwargs):
        nonlocal injected
        allowed = original(*args, **kwargs)
        if not injected:
            injected = True
            _suspend(kb_dir, gateway, "page_tree_selection", "newer pre-dispatch suspension")
        return allowed

    monkeypatch.setattr(
        desktop_page_tree_selection,
        "model_operation_dispatch_possible",
        suspend_after_precheck,
    )

    result = select_page_tree_evidence(
        kb_dir,
        "Compare Alpha and Beta",
        baseline.retrieval_plan,
        baseline.evidence,
        gateway,
        retry_scope=retry_scope,
    )

    assert result.degradation_reasons == ("page_tree_selection_suspended",)
    assert provider_calls == []
    assert _state(kb_dir, gateway, "page_tree_selection").status == "suspended"


def test_page_selection_retry_success_does_not_clear_a_newer_suspension(
    tmp_path, monkeypatch
) -> None:
    kb_dir = _knowledge_base(tmp_path)

    class SuspendDuringTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            _suspend(kb_dir, gateway, "page_tree_selection", "newer in-flight suspension")
            return _selection_response(request)

    gateway = _gateway(kb_dir, monkeypatch, SuspendDuringTransport)
    baseline = DesktopEvidenceRetriever(kb_dir).retrieve("Compare Alpha and Beta")
    _suspend(kb_dir, gateway, "page_tree_selection", "observed suspension")
    retry_scope = "grounded-answer:selection-in-flight-race"
    _authorize_contracts(kb_dir, gateway, retry_scope, "page_tree_selection")

    result = select_page_tree_evidence(
        kb_dir,
        "Compare Alpha and Beta",
        baseline.retrieval_plan,
        baseline.evidence,
        gateway,
        retry_scope=retry_scope,
    )

    assert result.degradation_reasons == ()
    state = _state(kb_dir, gateway, "page_tree_selection")
    assert state.status == "suspended"
    assert state.reason == "newer in-flight suspension"


def test_grounded_answer_preauthorizes_the_exact_retrieval_repair_contract(
    tmp_path, monkeypatch
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    exact_repair_digest = structured_output_repair_contract_digest("query_planning")
    repair_permit_visible_at_first_dispatch: list[bool] = []

    class InvalidThenValidTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            if request.operation == "query_planning":
                with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
                    repair_permit_visible_at_first_dispatch.append(
                        connection.execute(
                            """
                            SELECT 1 FROM model_operation_retry_permits
                            WHERE operation = 'structured_output_repair'
                                AND prompt_contract_digest = ?
                            """,
                            (exact_repair_digest,),
                        ).fetchone()
                        is not None
                    )
                return "not-json"
            if request.operation == "structured_output_repair":
                return _query_planning_response()
            if request.operation == "grounded_answer":
                return "Alpha is supported by the imported evidence."
            raise AssertionError(request.operation)

    gateway = _gateway(kb_dir, monkeypatch, InvalidThenValidTransport)
    _suspend(kb_dir, gateway, "query_planning", "observed parent suspension")
    profile = gateway.execution_profile_for_operation("structured_output_repair")
    DesktopModelOperationContractStore(kb_dir).suspend(
        operation="structured_output_repair",
        capability_identity=profile.capability_evidence_profile.identity,
        prompt_contract_digest=exact_repair_digest,
        failure_code="model_response_invalid",
        reason="observed repair suspension",
        failure_stage="domain_validation",
    )

    result = DesktopGroundedAnswerService(kb_dir, model_gateway=gateway).generate(
        "Alpha",
        retry_suspended_operations=True,
    )

    assert result.status == "completed"
    assert repair_permit_visible_at_first_dispatch == [True]
