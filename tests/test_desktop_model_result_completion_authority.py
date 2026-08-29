"""Completion authority at the model-operation readiness seam."""

from __future__ import annotations

from openkb.desktop_model_gateway import DesktopModelResult
from openkb.desktop_model_operation_state import DesktopModelOperationContractStore
from openkb.desktop_model_result_failure import (
    DesktopModelOperationCompletionAuthority,
    mark_model_result_operation_ready,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_ordinary_completion_authority_preserves_a_concurrent_suspension(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopModelOperationContractStore(kb_dir)
    contract = {
        "operation": "retrieval_plan",
        "capability_identity": "analysis-capability",
        "prompt_contract_digest": "retrieval-contract",
    }
    store.suspend(
        **contract,
        failure_code="model_response_invalid",
        reason="A newer request suspended this contract.",
        failure_stage="domain_validation",
    )
    result = DesktopModelResult(
        "call-1",
        '{"terms":["alpha"]}',
        1,
        diagnostic_context=dict(contract),
    )

    mark_model_result_operation_ready(
        kb_dir,
        object(),
        result,
        authority=DesktopModelOperationCompletionAuthority.ordinary(),
    )

    state = store.state(**contract)
    assert state.status == "suspended"
    assert state.reason == "A newer request suspended this contract."
