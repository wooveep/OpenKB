"""Reuse validated layout decisions only for identical evidence and model contracts."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openkb.knowledge.pages.page import knowledge_page_claim_snapshot_digest
from openkb.knowledge.pages.planning import (
    KnowledgePagePlanValidationError,
    parse_knowledge_page_plan,
)
from openkb.knowledge.pages.store import PlannedKnowledgePage, execution_profile_digest
from openkb.locks import kb_ingest_lock
from openkb.shared.canonical_json import canonical_json, canonical_json_digest
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

PAGE_PLAN_CACHE_MIGRATION_STATEMENTS = (
    "CREATE TABLE knowledge_page_plan_cache (identity_id TEXT PRIMARY KEY, "
    "binding_digest TEXT NOT NULL, template_json TEXT NOT NULL, provenance_json TEXT NOT NULL)",
)


class KnowledgePagePlanCache:
    def __init__(self, kb_dir: Path, prompt_digest: str, profile_json: str):
        self._kb_dir = kb_dir
        self._prompt_digest = prompt_digest
        self._profile_json = profile_json

    def _binding(self, inputs: dict[str, Any]) -> str:
        def facts(items, transient):
            return [{k: v for k, v in asdict(item).items() if k not in transient} for item in items]

        return canonical_json_digest(
            {
                "identity_id": inputs["identity_id"],
                "title": inputs["title"],
                "language": inputs["knowledge_language"],
                "prompt_digest": self._prompt_digest,
                "profile": self._profile_json,
                "claims": facts(inputs["claims"], {"generation_id", "claim_id"}),
                "relations": facts(inputs["relations"], {"generation_id", "assertion_id"}),
            }
        )

    def get(self, inputs: dict[str, Any]) -> PlannedKnowledgePage | None:
        with closing(connect_database(desktop_state_database_path(self._kb_dir))) as db:
            row = db.execute(
                "SELECT template_json, provenance_json FROM knowledge_page_plan_cache "
                "WHERE identity_id = ? AND binding_digest = ?",
                (inputs["identity_id"], self._binding(inputs)),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            _map_units(
                payload,
                lambda i: inputs["claims"][i].claim_id,
                lambda i: inputs["relations"][i].assertion_id,
            )
            payload.update(generation_id=inputs["generation_id"], identity_id=inputs["identity_id"])
            plan = parse_knowledge_page_plan(
                canonical_json(payload),
                expected_generation_id=inputs["generation_id"],
                expected_identity_id=inputs["identity_id"],
                claim_snapshot_digest=knowledge_page_claim_snapshot_digest(inputs["claims"]),
                eligible_claim_ids=tuple(c.claim_id for c in inputs["claims"]),
                available_relation_assertion_ids=frozenset(
                    r.assertion_id for r in inputs["relations"]
                ),
            )
            provenance = json.loads(row[1])
            provenance["reused_for_generation_id"] = inputs["generation_id"]
            return PlannedKnowledgePage(
                plan,
                "knowledge_page_planning",
                self._prompt_digest,
                self._profile_json,
                execution_profile_digest(self._profile_json),
                canonical_json(provenance),
            )
        except (ValueError, KeyError, IndexError, TypeError, KnowledgePagePlanValidationError):
            return None

    def put(self, inputs: dict[str, Any], planned: PlannedKnowledgePage) -> None:
        claims = {c.claim_id: i for i, c in enumerate(inputs["claims"])}
        relations = {r.assertion_id: i for i, r in enumerate(inputs["relations"])}
        payload = {
            "lead": asdict(planned.plan.lead) if planned.plan.lead else None,
            "sections": [asdict(s) for s in planned.plan.sections],
        }
        _map_units(payload, claims.__getitem__, relations.__getitem__)
        with (
            kb_ingest_lock(desktop_state_dir(self._kb_dir)),
            closing(connect_database(desktop_state_database_path(self._kb_dir))) as db,
        ):
            with db:
                db.execute(
                    "INSERT INTO knowledge_page_plan_cache VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(identity_id) DO UPDATE SET "
                    "binding_digest = excluded.binding_digest, "
                    "template_json = excluded.template_json, "
                    "provenance_json = excluded.provenance_json",
                    (
                        inputs["identity_id"],
                        self._binding(inputs),
                        canonical_json(payload),
                        planned.planner_provenance_json,
                    ),
                )


def _map_units(payload, claim_ref, relation_ref):
    def unit(value):
        value.pop("unit_id", None)
        value["claim_ids"] = [claim_ref(i) for i in value["claim_ids"]]
        value["relation_assertion_ids"] = [relation_ref(i) for i in value["relation_assertion_ids"]]

    def sections(values):
        for section in values:
            section.pop("section_id", None)
            for value in section["units"]:
                unit(value)
            sections(section["sections"])

    if payload["lead"] is not None:
        unit(payload["lead"])
    sections(payload["sections"])
