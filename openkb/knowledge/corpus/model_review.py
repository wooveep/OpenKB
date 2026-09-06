"""Bounded semantic comparisons before corpus activation, outside SQLite transactions."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from openkb.knowledge.corpus.review_store import (
    CorpusReviewService,
    has_nonliteral_cross_document_claims,
    record_review_in,
    snapshot_is_current_in,
)
from openkb.locks import kb_ingest_lock
from openkb.models.execution_profile import estimate_model_tokens
from openkb.models.gateway import DesktopModelCallError, DesktopModelCancelledError
from openkb.models.prompt_contracts import prompt_contract_for
from openkb.models.result_failure import (
    DesktopModelOperationCompletionAuthority,
    DesktopModelOperationSuspendedError,
    mark_structured_output_operations_ready,
    require_model_operation_dispatch,
    suspend_analysis_operation_failure,
)
from openkb.models.structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)
from openkb.shared.canonical_json import canonical_json
from openkb.shared.clock import timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir


def review_pending_claims(
    kb_dir: Path, gateway, should_stop, retry_scope=None, can_dispatch=lambda: True
) -> None:
    from openkb.knowledge.corpus.candidates import load_admitted_candidates_in
    from openkb.knowledge.corpus.knowledge import _candidate_clusters
    from openkb.knowledge.corpus.synthesis_generation import capture_corpus_candidate_inputs_in

    with (
        kb_ingest_lock(desktop_state_dir(kb_dir)),
        closing(connect_database(desktop_state_database_path(kb_dir))) as db,
    ):
        with db:
            candidates = load_admitted_candidates_in(db, capture_corpus_candidate_inputs_in(db))
            for cluster in _candidate_clusters(candidates, db):
                if has_nonliteral_cross_document_claims(cluster):
                    record_review_in(db, cluster, "claim_relationship_review", timestamp())
    pending = [
        item
        for item in CorpusReviewService(kb_dir).list_items()
        if item["reason"] == "claim_relationship_review" and item["authority"] is None
    ]
    for item in pending[:8]:
        if should_stop():
            raise DesktopModelCancelledError()
        payload = canonical_json(item)
        resolver = getattr(gateway, "execution_profile_for_operation", None)
        profile = resolver("knowledge_claim_review") if callable(resolver) else None
        input_budget = min(12_000, getattr(profile, "document_input_budget_tokens", 12_000))
        if len(payload.encode("utf-8")) > 48_000 or estimate_model_tokens(payload) > input_budget:
            continue  # Keep the complete snapshot available for human review.

        def validate(content):
            result = json.loads(content)
            if not isinstance(result, dict) or set(result) != {
                "review_id",
                "verdict",
                "evidence_ids",
            }:
                raise ValueError("invalid_claim_review_shape")
            if result["review_id"] != item["review_id"] or result["verdict"] not in (
                "compatible",
                "conflicting",
                "unresolved",
            ):
                raise ValueError("invalid_claim_review_binding")
            sources = result["evidence_ids"]
            known = {source["evidence_id"] for source in item["evidence"]}
            if (
                not isinstance(sources, list)
                or not sources
                or any(not isinstance(s, str) or s not in known for s in sources)
            ):
                raise ValueError("invalid_claim_review_sources")
            if any(
                not set(sources).intersection(
                    eid for claim in c["claims"] for eid in claim["evidence_ids"]
                )
                for c in item["candidates"]
            ):
                raise ValueError("claim_review_must_cover_every_candidate")
            return result

        def invoke(request):
            if not can_dispatch():
                raise DesktopModelCancelledError()
            require_model_operation_dispatch(kb_dir, gateway, request, retry_scope=retry_scope)
            return gateway.analyze(request, on_event=lambda _event: None, is_cancelled=should_stop)

        try:
            output = run_structured_output(
                operation="knowledge_claim_review",
                document_name="Corpus claim comparison",
                source_material=payload,
                invoke=invoke,
                validate=validate,
            )
        except DesktopModelCancelledError:
            raise
        except (DesktopStructuredOutputInvalidError, DesktopModelOperationSuspendedError):
            continue
        except DesktopModelCallError as error:
            suspend_analysis_operation_failure(kb_dir, gateway, error)
            continue
        if should_stop():
            raise DesktopModelCancelledError()
        with (
            kb_ingest_lock(desktop_state_dir(kb_dir)),
            closing(connect_database(desktop_state_database_path(kb_dir))) as db,
        ):
            with db:
                if not snapshot_is_current_in(db, item):
                    continue
                verdict = output.value["verdict"]
                db.execute(
                    "UPDATE knowledge_identity_review_items SET decision = ?, authority = 'model', "
                    "status = ?, resolved_at = ?, provenance_json = ? "
                    "WHERE review_id = ? AND authority IS NULL",
                    (
                        verdict,
                        "resolved" if verdict == "compatible" else "pending",
                        timestamp(),
                        canonical_json(
                            {
                                "call_id": output.result.call_id,
                                "prompt_digest": prompt_contract_for(
                                    "knowledge_claim_review"
                                ).digest,
                                "repaired": output.repaired,
                                "evidence_ids": output.value["evidence_ids"],
                            }
                        ),
                        item["review_id"],
                    ),
                )
        mark_structured_output_operations_ready(
            kb_dir,
            gateway,
            output,
            authority=DesktopModelOperationCompletionAuthority.for_retry_scope(retry_scope),
        )
