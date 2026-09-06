"""Model-backed adapter for the domain-neutral Knowledge Page planner boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from openkb.knowledge.pages.plan_cache import KnowledgePagePlanCache
from openkb.knowledge.pages.planner import run_knowledge_page_planning
from openkb.knowledge.pages.planning import KnowledgePagePlan
from openkb.knowledge.pages.store import (
    KnowledgePagePlanner,
    PlannedKnowledgePage,
    execution_profile_digest,
)
from openkb.models.gateway import DesktopModelCancelledError, DesktopModelGateway
from openkb.models.prompt_contracts import prompt_contract_for
from openkb.models.result_failure import require_model_operation_dispatch
from openkb.models.structured_output import DesktopValidatedStructuredOutput


def model_knowledge_page_planner(
    gateway: DesktopModelGateway,
    *,
    should_stop: Callable[[], bool],
    kb_dir: Path | None = None,
    retry_scope: str | None = None,
    completed_outputs: list[DesktopValidatedStructuredOutput[KnowledgePagePlan]] | None = None,
    on_model_event: Callable[[object], None] | None = None,
    can_dispatch: Callable[[], bool] = lambda: True,
) -> KnowledgePagePlanner:
    """Bind a gateway to the validated, fact-free page-planning interface."""
    contract_digest = prompt_contract_for("knowledge_page_planning").digest
    profile_json = _execution_profile_json(gateway)
    cache = KnowledgePagePlanCache(kb_dir, contract_digest, profile_json) if kb_dir else None

    def plan(**kwargs) -> PlannedKnowledgePage:
        if cache is not None:
            cached = cache.get(kwargs)
            if cached is not None:
                return cached

        def invoke(request):
            if not can_dispatch():
                raise DesktopModelCancelledError()
            if kb_dir is not None:
                require_model_operation_dispatch(
                    kb_dir,
                    gateway,
                    request,
                    retry_scope=retry_scope,
                )
            return gateway.analyze(
                request,
                on_event=on_model_event or (lambda _event: None),
                is_cancelled=should_stop,
            )

        run = run_knowledge_page_planning(**kwargs, invoke=invoke)
        if completed_outputs is not None:
            completed_outputs.append(run.output)
        provenance = json.dumps(
            {
                "provider": gateway.provider_name,
                "model": gateway.model_name,
                "call_id": run.result.call_id,
                "response_sha256": hashlib.sha256(run.result.content.encode("utf-8")).hexdigest(),
                "repaired": run.repaired,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        planned = PlannedKnowledgePage(
            plan=run.plan,
            planning_operation="knowledge_page_planning",
            prompt_contract_digest=contract_digest,
            execution_profile_json=profile_json,
            execution_profile_digest=execution_profile_digest(profile_json),
            planner_provenance_json=provenance,
        )
        if cache is not None:
            cache.put(kwargs, planned)
        return planned

    return plan


def _execution_profile_json(gateway: DesktopModelGateway) -> str:
    resolver = getattr(gateway, "execution_profile_for_operation", None)
    if callable(resolver):
        value = resolver("knowledge_page_planning").as_dict()
    else:
        value = {
            "provider": gateway.provider_name,
            "model": gateway.model_name,
            "operation": "knowledge_page_planning",
            "profile_state": "transport_only",
        }
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
