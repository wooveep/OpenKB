"""Generation-owned persistence and item-scoped outcomes for Knowledge Page Plans."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from openkb.knowledge.pages.page import (
    KnowledgePageClaimSnapshot,
    KnowledgePageRelationSnapshot,
    knowledge_page_claim_id,
    knowledge_page_claim_snapshot_digest,
    render_knowledge_page,
)
from openkb.knowledge.pages.planning import (
    KnowledgePagePlan,
    KnowledgePagePlanValidationError,
    KnowledgePageSection,
    KnowledgePageUnit,
)

KnowledgePageOutcomeStatus = Literal["ready", "deferred", "carried_forward"]


@dataclass(frozen=True)
class PlannedKnowledgePage:
    plan: KnowledgePagePlan
    planning_operation: str
    prompt_contract_digest: str
    execution_profile_json: str
    execution_profile_digest: str
    planner_provenance_json: str


@dataclass(frozen=True)
class DeferredKnowledgePage:
    identity_id: str
    claim_snapshot_digest: str
    error_codes: tuple[str, ...]


@dataclass(frozen=True)
class PublishedKnowledgePage:
    generation_id: int
    identity_id: str
    status: KnowledgePageOutcomeStatus
    claim_snapshot_digest: str
    published_generation_id: int | None
    error_codes: tuple[str, ...]
    plan: KnowledgePagePlan | None
    rendered_content_digest: str | None
    factual_unit_count: int


class KnowledgePagePlanner(Protocol):
    def __call__(
        self,
        *,
        document_name: str,
        generation_id: int,
        identity_id: str,
        title: str,
        claims: tuple[KnowledgePageClaimSnapshot, ...],
        relations: tuple[KnowledgePageRelationSnapshot, ...],
        knowledge_language: str,
    ) -> PlannedKnowledgePage: ...


def build_generation_knowledge_pages_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    knowledge_language: str,
    now: str,
    outcomes: Mapping[str, PlannedKnowledgePage | DeferredKnowledgePage],
) -> tuple[str, ...]:
    """Publish valid pages and isolate invalid identities without semantic fallback."""
    from openkb.knowledge.pages.generations import knowledge_content_sha256

    connection.execute(
        "DELETE FROM knowledge_generation_page_outcomes WHERE generation_id = ?",
        (generation_id,),
    )
    rows = connection.execute(
        "SELECT identity_id, item_key, title FROM knowledge_generation_items "
        "WHERE generation_id = ? AND identity_id IS NOT NULL ORDER BY item_key",
        (generation_id,),
    ).fetchall()
    integrity_issues: list[str] = []
    for row in rows:
        identity_id = str(row[0])
        item_key = str(row[1])
        claims = knowledge_page_claims_for_identity_in(connection, generation_id, identity_id)
        relations = knowledge_page_relations_for_identity_in(connection, generation_id, identity_id)
        snapshot_digest = knowledge_page_claim_snapshot_digest(claims)
        outcome = outcomes.get(identity_id)
        if isinstance(outcome, PlannedKnowledgePage):
            try:
                if outcome.plan.claim_snapshot_digest != snapshot_digest:
                    raise KnowledgePagePlanValidationError(("claim_snapshot_digest_mismatch",))
                rendered = render_knowledge_page(outcome.plan, claims, relations=relations)
                if not rendered.markdown:
                    raise KnowledgePagePlanValidationError(("empty_rendered_page",))
                _persist_ready_page_in(
                    connection,
                    planned=outcome,
                    rendered_content_digest=rendered.content_digest,
                    factual_unit_count=rendered.factual_unit_count,
                    now=now,
                )
                connection.execute(
                    "UPDATE knowledge_generation_items SET content_markdown = ?, "
                    "content_sha256 = ? WHERE generation_id = ? AND item_key = ?",
                    (
                        rendered.markdown,
                        knowledge_content_sha256(rendered.markdown),
                        generation_id,
                        item_key,
                    ),
                )
                continue
            except (KnowledgePagePlanValidationError, KeyError) as error:
                error_codes = (
                    error.issues
                    if isinstance(error, KnowledgePagePlanValidationError)
                    else ("unknown_plan_reference",)
                )
        elif isinstance(outcome, DeferredKnowledgePage):
            error_codes = outcome.error_codes
            if outcome.claim_snapshot_digest != snapshot_digest:
                error_codes = (*error_codes, "claim_snapshot_digest_mismatch")
        else:
            error_codes = ("missing_page_plan",)
        _persist_deferred_page_in(
            connection,
            generation_id=generation_id,
            identity_id=identity_id,
            item_key=item_key,
            claim_snapshot_digest=snapshot_digest,
            error_codes=tuple(dict.fromkeys(error_codes)),
            now=now,
        )
    connection.execute(
        "UPDATE knowledge_generation_manifests SET page_state = 'ready', updated_at = ? "
        "WHERE generation_id = ?",
        (now, generation_id),
    )
    integrity_issues.extend(generation_knowledge_page_issues_in(connection, generation_id))
    return tuple(dict.fromkeys(integrity_issues))


def knowledge_page_claims_for_identity_in(
    connection: sqlite3.Connection,
    generation_id: int,
    identity_id: str,
) -> tuple[KnowledgePageClaimSnapshot, ...]:
    """Load and exactly consolidate the generation's evidence-bound claim snapshot."""
    rows = connection.execute(
        """
        SELECT mappings.candidate_generation_id, mappings.candidate_id,
            claims.claim_ordinal, claims.claim_text, claims.applicability_json,
            sources.evidence_id
        FROM knowledge_generation_identity_mappings AS mappings
        JOIN knowledge_candidate_generation_claims AS claims
          ON claims.candidate_generation_id = mappings.candidate_generation_id
         AND claims.candidate_id = mappings.candidate_id
        JOIN knowledge_candidate_generation_claim_sources AS sources
          ON sources.candidate_generation_id = claims.candidate_generation_id
         AND sources.candidate_id = claims.candidate_id
         AND sources.claim_ordinal = claims.claim_ordinal
        WHERE mappings.generation_id = ? AND mappings.identity_id = ?
        ORDER BY mappings.candidate_generation_id, mappings.candidate_id,
            claims.claim_ordinal, sources.evidence_id
        """,
        (generation_id, identity_id),
    ).fetchall()
    grouped_origins: dict[tuple[str, str, int], list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        grouped_origins[(str(row[0]), str(row[1]), int(row[2]))].append(row)
    consolidated: dict[
        tuple[str, tuple[tuple[str, str], ...]],
        tuple[tuple[str, str, int], str, tuple[tuple[str, str], ...], set[str]],
    ] = {}
    for origin, values in grouped_origins.items():
        text = str(values[0][3])
        applicability = _applicability(str(values[0][4]))
        key = (" ".join(text.split()).casefold(), applicability)
        existing = consolidated.get(key)
        evidence_ids = {str(value[5]) for value in values}
        if existing is None:
            consolidated[key] = (origin, text, applicability, evidence_ids)
        else:
            existing[3].update(evidence_ids)
    claims: list[KnowledgePageClaimSnapshot] = []
    for origin, text, applicability, evidence_ids in consolidated.values():
        claims.append(
            KnowledgePageClaimSnapshot(
                generation_id=generation_id,
                identity_id=identity_id,
                candidate_generation_id=origin[0],
                candidate_id=origin[1],
                claim_ordinal=origin[2],
                claim_id=knowledge_page_claim_id(
                    generation_id,
                    identity_id,
                    text,
                    applicability,
                ),
                text=text,
                applicability=applicability,
                evidence_ids=tuple(sorted(evidence_ids)),
            )
        )
    return tuple(claims)


def knowledge_page_relations_for_identity_in(
    connection: sqlite3.Connection,
    generation_id: int,
    identity_id: str,
) -> tuple[KnowledgePageRelationSnapshot, ...]:
    """Load current-epoch relation assertions when the optional graph is available."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'knowledge_generation_relation_assertions'"
    ).fetchone()
    if table is None:
        return ()
    rows = connection.execute(
        """
        SELECT assertions.assertion_id, assertions.source_identity_id, source.title,
            assertions.target_identity_id, target.title, assertions.label,
            sources.evidence_id
        FROM knowledge_generation_relation_assertions AS assertions
        JOIN knowledge_generation_items AS source
          ON source.generation_id = assertions.generation_id
         AND source.identity_id = assertions.source_identity_id
        JOIN knowledge_generation_items AS target
          ON target.generation_id = assertions.generation_id
         AND target.identity_id = assertions.target_identity_id
        JOIN knowledge_generation_relation_sources AS sources
          ON sources.generation_id = assertions.generation_id
         AND sources.assertion_id = assertions.assertion_id
        WHERE assertions.generation_id = ?
          AND (? IN (assertions.source_identity_id, assertions.target_identity_id))
        ORDER BY assertions.assertion_id, sources.evidence_id
        """,
        (generation_id, identity_id),
    ).fetchall()
    grouped: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[0])].append(row)
    return tuple(
        KnowledgePageRelationSnapshot(
            generation_id=generation_id,
            assertion_id=assertion_id,
            source_identity_id=str(values[0][1]),
            source_title=str(values[0][2]),
            target_identity_id=str(values[0][3]),
            target_title=str(values[0][4]),
            label=str(values[0][5]),
            evidence_ids=tuple(str(value[6]) for value in values),
        )
        for assertion_id, values in grouped.items()
    )


def generation_knowledge_pages_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[PublishedKnowledgePage, ...]:
    rows = connection.execute(
        "SELECT identity_id, status, claim_snapshot_digest, published_generation_id, "
        "error_codes_json FROM knowledge_generation_page_outcomes "
        "WHERE generation_id = ? ORDER BY identity_id",
        (generation_id,),
    ).fetchall()
    result: list[PublishedKnowledgePage] = []
    for row in rows:
        identity_id = str(row[0])
        plan_row = connection.execute(
            "SELECT rendered_content_digest, factual_unit_count FROM "
            "knowledge_generation_page_plans WHERE generation_id = ? AND identity_id = ?",
            (generation_id, identity_id),
        ).fetchone()
        plan = _plan_in(connection, generation_id, identity_id, str(row[2]))
        result.append(
            PublishedKnowledgePage(
                generation_id=generation_id,
                identity_id=identity_id,
                status=str(row[1]),  # type: ignore[arg-type]
                claim_snapshot_digest=str(row[2]),
                published_generation_id=int(row[3]) if row[3] is not None else None,
                error_codes=_strings(str(row[4])),
                plan=plan,
                rendered_content_digest=str(plan_row[0]) if plan_row is not None else None,
                factual_unit_count=int(plan_row[1]) if plan_row is not None else 0,
            )
        )
    return tuple(result)


def generation_knowledge_page_issues_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[str, ...]:
    """Revalidate plans, rendered items, carry-forward, and omission invariants."""
    from openkb.knowledge.pages.generations import knowledge_content_sha256

    issues: list[str] = []
    pages = generation_knowledge_pages_in(connection, generation_id)
    missing_outcomes = connection.execute(
        "SELECT COUNT(*) FROM knowledge_generation_items AS items "
        "WHERE items.generation_id = ? AND items.identity_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM knowledge_generation_page_outcomes AS outcomes "
        "WHERE outcomes.generation_id = items.generation_id "
        "AND outcomes.identity_id = items.identity_id)",
        (generation_id,),
    ).fetchone()
    if missing_outcomes is not None and int(missing_outcomes[0]) > 0:
        issues.append("knowledge_page_outcomes_incomplete")
    for page in pages:
        item = connection.execute(
            "SELECT content_markdown, content_sha256 FROM knowledge_generation_items "
            "WHERE generation_id = ? AND identity_id = ?",
            (generation_id, page.identity_id),
        ).fetchone()
        if page.status == "deferred":
            if item is not None or page.plan is not None:
                issues.append(f"knowledge_page:{page.identity_id}:invalid_deferred_item")
            continue
        if page.status == "carried_forward":
            if item is None or not _carried_item_matches_in(connection, page):
                issues.append(f"knowledge_page:{page.identity_id}:invalid_carry_forward")
            continue
        claims = knowledge_page_claims_for_identity_in(connection, generation_id, page.identity_id)
        relations = knowledge_page_relations_for_identity_in(
            connection, generation_id, page.identity_id
        )
        if page.plan is None or item is None:
            issues.append(f"knowledge_page:{page.identity_id}:ready_item_missing")
            continue
        try:
            rendered = render_knowledge_page(page.plan, claims, relations=relations)
        except KnowledgePagePlanValidationError as error:
            issues.extend(f"knowledge_page:{page.identity_id}:{issue}" for issue in error.issues)
            continue
        if page.plan.digest != _stored_plan_digest_in(connection, generation_id, page.identity_id):
            issues.append(f"knowledge_page:{page.identity_id}:plan_digest_mismatch")
        if rendered.content_digest != page.rendered_content_digest:
            issues.append(f"knowledge_page:{page.identity_id}:render_digest_mismatch")
        if str(item[0]) != rendered.markdown or str(item[1]) != knowledge_content_sha256(
            rendered.markdown
        ):
            issues.append(f"knowledge_page:{page.identity_id}:rendered_item_mismatch")
    return tuple(dict.fromkeys(issues))


def _persist_ready_page_in(
    connection: sqlite3.Connection,
    *,
    planned: PlannedKnowledgePage,
    rendered_content_digest: str,
    factual_unit_count: int,
    now: str,
) -> None:
    plan = planned.plan
    connection.execute(
        "INSERT INTO knowledge_generation_page_outcomes "
        "(generation_id, identity_id, status, claim_snapshot_digest, "
        "published_generation_id, error_codes_json, created_at) "
        "VALUES (?, ?, 'ready', ?, ?, '[]', ?)",
        (
            plan.generation_id,
            plan.identity_id,
            plan.claim_snapshot_digest,
            plan.generation_id,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO knowledge_generation_page_plans (
            generation_id, identity_id, claim_snapshot_digest, plan_digest,
            planning_operation, prompt_contract_digest, execution_profile_json,
            execution_profile_digest, planner_provenance_json,
            rendered_content_digest, factual_unit_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan.generation_id,
            plan.identity_id,
            plan.claim_snapshot_digest,
            plan.digest,
            planned.planning_operation,
            planned.prompt_contract_digest,
            planned.execution_profile_json,
            planned.execution_profile_digest,
            planned.planner_provenance_json,
            rendered_content_digest,
            factual_unit_count,
            now,
        ),
    )
    if plan.lead is not None:
        _persist_unit_in(connection, plan, plan.lead, section_id=None, ordinal=0)
    for ordinal, section in enumerate(plan.sections):
        _persist_section_in(
            connection,
            plan,
            section,
            parent_section_id=None,
            ordinal=ordinal,
            depth=1,
        )


def _persist_section_in(
    connection: sqlite3.Connection,
    plan: KnowledgePagePlan,
    section: KnowledgePageSection,
    *,
    parent_section_id: str | None,
    ordinal: int,
    depth: int,
) -> None:
    connection.execute(
        "INSERT INTO knowledge_generation_page_sections "
        "(generation_id, identity_id, section_id, parent_section_id, "
        "section_ordinal, depth, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            plan.generation_id,
            plan.identity_id,
            section.section_id,
            parent_section_id,
            ordinal,
            depth,
            section.title,
        ),
    )
    for unit_ordinal, unit in enumerate(section.units):
        _persist_unit_in(
            connection,
            plan,
            unit,
            section_id=section.section_id,
            ordinal=unit_ordinal,
        )
    for child_ordinal, child in enumerate(section.sections):
        _persist_section_in(
            connection,
            plan,
            child,
            parent_section_id=section.section_id,
            ordinal=child_ordinal,
            depth=depth + 1,
        )


def _persist_unit_in(
    connection: sqlite3.Connection,
    plan: KnowledgePagePlan,
    unit: KnowledgePageUnit,
    *,
    section_id: str | None,
    ordinal: int,
) -> None:
    connection.execute(
        "INSERT INTO knowledge_generation_page_units "
        "(generation_id, identity_id, unit_id, section_id, unit_ordinal, presentation) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan.generation_id,
            plan.identity_id,
            unit.unit_id,
            section_id,
            ordinal,
            unit.presentation,
        ),
    )
    connection.executemany(
        "INSERT INTO knowledge_generation_page_unit_claims "
        "(generation_id, identity_id, unit_id, claim_id, claim_ordinal) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            (plan.generation_id, plan.identity_id, unit.unit_id, claim_id, index)
            for index, claim_id in enumerate(unit.claim_ids)
        ),
    )
    connection.executemany(
        "INSERT INTO knowledge_generation_page_unit_relations "
        "(generation_id, identity_id, unit_id, assertion_id, relation_ordinal) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            (plan.generation_id, plan.identity_id, unit.unit_id, assertion_id, index)
            for index, assertion_id in enumerate(unit.relation_assertion_ids)
        ),
    )


def _persist_deferred_page_in(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    identity_id: str,
    item_key: str,
    claim_snapshot_digest: str,
    error_codes: tuple[str, ...],
    now: str,
) -> None:
    parent = connection.execute(
        "SELECT parent_generation_id FROM knowledge_generation_manifests WHERE generation_id = ?",
        (generation_id,),
    ).fetchone()
    parent_generation_id = int(parent[0]) if parent is not None and parent[0] is not None else None
    carried = (
        parent_generation_id
        if parent_generation_id is not None
        and _carry_forward_item_in(
            connection,
            parent_generation_id=parent_generation_id,
            generation_id=generation_id,
            identity_id=identity_id,
            item_key=item_key,
        )
        else None
    )
    status = "carried_forward" if carried is not None else "deferred"
    if carried is None:
        connection.execute(
            "DELETE FROM knowledge_generation_items WHERE generation_id = ? AND item_key = ?",
            (generation_id, item_key),
        )
    connection.execute(
        "INSERT INTO knowledge_generation_page_outcomes "
        "(generation_id, identity_id, status, claim_snapshot_digest, "
        "published_generation_id, error_codes_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            generation_id,
            identity_id,
            status,
            claim_snapshot_digest,
            carried,
            _json(error_codes),
            now,
        ),
    )


def _carry_forward_item_in(
    connection: sqlite3.Connection,
    *,
    parent_generation_id: int,
    generation_id: int,
    identity_id: str,
    item_key: str,
) -> bool:
    prior = connection.execute(
        "SELECT content_markdown, content_sha256 FROM knowledge_generation_items "
        "WHERE generation_id = ? AND identity_id = ?",
        (parent_generation_id, identity_id),
    ).fetchone()
    if prior is None:
        return False
    invalid_source = connection.execute(
        """
        SELECT 1 FROM knowledge_generation_item_sources AS sources
        WHERE sources.generation_id = ? AND sources.item_key = (
            SELECT item_key FROM knowledge_generation_items
            WHERE generation_id = ? AND identity_id = ?
        ) AND NOT EXISTS (
            SELECT 1 FROM evidence_occurrences AS occurrences
            JOIN source_documents AS documents
              ON documents.document_id = occurrences.document_id
            WHERE occurrences.evidence_id = sources.evidence_id
              AND documents.availability = 'available'
        ) LIMIT 1
        """,
        (parent_generation_id, parent_generation_id, identity_id),
    ).fetchone()
    if invalid_source is not None:
        return False
    connection.execute(
        "UPDATE knowledge_generation_items SET content_markdown = ?, content_sha256 = ? "
        "WHERE generation_id = ? AND item_key = ?",
        (str(prior[0]), str(prior[1]), generation_id, item_key),
    )
    connection.execute(
        "DELETE FROM knowledge_generation_item_sources WHERE generation_id = ? AND item_key = ?",
        (generation_id, item_key),
    )
    connection.execute(
        """
        INSERT INTO knowledge_generation_item_sources (
            generation_id, item_key, source_id, evidence_id, claim_text
        )
        SELECT ?, ?, source_id, evidence_id, claim_text
        FROM knowledge_generation_item_sources
        WHERE generation_id = ? AND item_key = (
            SELECT item_key FROM knowledge_generation_items
            WHERE generation_id = ? AND identity_id = ?
        )
        """,
        (generation_id, item_key, parent_generation_id, parent_generation_id, identity_id),
    )
    return True


def _plan_in(
    connection: sqlite3.Connection,
    generation_id: int,
    identity_id: str,
    claim_snapshot_digest: str,
) -> KnowledgePagePlan | None:
    row = connection.execute(
        "SELECT plan_digest FROM knowledge_generation_page_plans "
        "WHERE generation_id = ? AND identity_id = ?",
        (generation_id, identity_id),
    ).fetchone()
    if row is None:
        return None
    units = _units_in(connection, generation_id, identity_id)
    lead = next((unit for section_id, _ordinal, unit in units if section_id is None), None)
    section_rows = connection.execute(
        "SELECT section_id, parent_section_id, section_ordinal, title "
        "FROM knowledge_generation_page_sections "
        "WHERE generation_id = ? AND identity_id = ? "
        "ORDER BY depth, parent_section_id, section_ordinal",
        (generation_id, identity_id),
    ).fetchall()
    children: dict[str | None, list[tuple[object, ...]]] = defaultdict(list)
    for section_row in section_rows:
        parent_id = str(section_row[1]) if section_row[1] is not None else None
        children[parent_id].append(section_row)

    def section_from(section_row: tuple[object, ...]) -> KnowledgePageSection:
        section_id = str(section_row[0])
        return KnowledgePageSection(
            section_id=section_id,
            title=str(section_row[3]),
            units=tuple(
                unit for unit_section_id, _ordinal, unit in units if unit_section_id == section_id
            ),
            sections=tuple(section_from(child) for child in children.get(section_id, ())),
        )

    return KnowledgePagePlan(
        generation_id=generation_id,
        identity_id=identity_id,
        claim_snapshot_digest=claim_snapshot_digest,
        lead=lead,
        sections=tuple(section_from(section) for section in children.get(None, ())),
        digest=str(row[0]),
    )


def _units_in(
    connection: sqlite3.Connection,
    generation_id: int,
    identity_id: str,
) -> tuple[tuple[str | None, int, KnowledgePageUnit], ...]:
    rows = connection.execute(
        "SELECT unit_id, section_id, unit_ordinal, presentation "
        "FROM knowledge_generation_page_units "
        "WHERE generation_id = ? AND identity_id = ? "
        "ORDER BY section_id, unit_ordinal",
        (generation_id, identity_id),
    ).fetchall()
    result: list[tuple[str | None, int, KnowledgePageUnit]] = []
    for row in rows:
        unit_id = str(row[0])
        claim_ids = tuple(
            str(value[0])
            for value in connection.execute(
                "SELECT claim_id FROM knowledge_generation_page_unit_claims "
                "WHERE generation_id = ? AND identity_id = ? AND unit_id = ? "
                "ORDER BY claim_ordinal",
                (generation_id, identity_id, unit_id),
            )
        )
        relation_ids = tuple(
            str(value[0])
            for value in connection.execute(
                "SELECT assertion_id FROM knowledge_generation_page_unit_relations "
                "WHERE generation_id = ? AND identity_id = ? AND unit_id = ? "
                "ORDER BY relation_ordinal",
                (generation_id, identity_id, unit_id),
            )
        )
        result.append(
            (
                str(row[1]) if row[1] is not None else None,
                int(row[2]),
                KnowledgePageUnit(
                    unit_id=unit_id,
                    presentation=str(row[3]),  # type: ignore[arg-type]
                    claim_ids=claim_ids,
                    relation_assertion_ids=relation_ids,
                ),
            )
        )
    return tuple(result)


def _carried_item_matches_in(
    connection: sqlite3.Connection,
    page: PublishedKnowledgePage,
) -> bool:
    if page.published_generation_id is None:
        return False
    rows = connection.execute(
        "SELECT generation_id, content_markdown, content_sha256 "
        "FROM knowledge_generation_items WHERE identity_id = ? "
        "AND generation_id IN (?, ?) ORDER BY generation_id",
        (page.identity_id, page.generation_id, page.published_generation_id),
    ).fetchall()
    return len(rows) == 2 and (str(rows[0][1]), str(rows[0][2])) == (
        str(rows[1][1]),
        str(rows[1][2]),
    )


def _stored_plan_digest_in(
    connection: sqlite3.Connection,
    generation_id: int,
    identity_id: str,
) -> str:
    row = connection.execute(
        "SELECT plan_digest FROM knowledge_generation_page_plans "
        "WHERE generation_id = ? AND identity_id = ?",
        (generation_id, identity_id),
    ).fetchone()
    return str(row[0]) if row is not None else ""


def _applicability(value: str) -> tuple[tuple[str, str], ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise KnowledgePagePlanValidationError(("invalid_claim_applicability",))
    result: list[tuple[str, str]] = []
    for entry in parsed:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"dimension", "value", "source_evidence_ids"}
            or not isinstance(entry.get("dimension"), str)
            or not isinstance(entry.get("value"), str)
        ):
            raise KnowledgePagePlanValidationError(("invalid_claim_applicability",))
        result.append((str(entry["dimension"]), str(entry["value"])))
    return tuple(sorted(result))


def _strings(value: str) -> tuple[str, ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise KnowledgePagePlanValidationError(("invalid_persisted_identifiers",))
    return tuple(parsed)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def execution_profile_digest(execution_profile_json: str) -> str:
    """Digest one canonical profile payload without assigning provider semantics."""
    return hashlib.sha256(execution_profile_json.encode("utf-8")).hexdigest()
