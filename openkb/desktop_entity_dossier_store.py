"""Generation-owned persistence for validated Entity Dossier plans and facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from openkb.desktop_entity_dossier import (
    DossierClaimSnapshot,
    DossierPlanValidationError,
    EntityDossierPlan,
    EntityDossierSection,
    EntityDossierUnit,
    candidate_claim_id,
    entity_dossier_plan_digest,
    plan_entity_dossier,
    render_entity_dossier,
    validate_entity_dossier_plan,
)

_DETERMINISTIC_PLANNER_DIGEST = hashlib.sha256(
    b"openkb.entity-dossier.deterministic-planner.v1"
).hexdigest()


@dataclass(frozen=True)
class PublishedEntityDossier:
    generation_id: int
    identity_id: str
    status: str
    plan: EntityDossierPlan
    plan_digest: str
    rendered_content_digest: str
    fact_count: int
    language: Literal["en", "zh"]


@dataclass(frozen=True)
class PlannedEntityDossier:
    plan: EntityDossierPlan
    planning_operation: str
    prompt_contract_digest: str
    planner_provenance_json: str


class EntityDossierPlanner(Protocol):
    def __call__(
        self,
        *,
        document_name: str,
        generation_id: int,
        identity_id: str,
        claims: tuple[DossierClaimSnapshot, ...],
        language: Literal["en", "zh"],
        known_related_identity_ids: frozenset[str],
    ) -> PlannedEntityDossier: ...


def build_generation_entity_dossiers_in(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    language: Literal["en", "zh"],
    now: str,
    planned: Mapping[str, EntityDossierPlan] | None = None,
    planning_operation: str = "entity_dossier_planning",
    prompt_contract_digest: str = _DETERMINISTIC_PLANNER_DIGEST,
    planner_provenance_json: str = '{"planner":"deterministic"}',
    planner: EntityDossierPlanner | None = None,
) -> tuple[str, ...]:
    """Rebuild every generated Entity from the generation's structured claims."""
    from openkb.desktop_knowledge_generations import knowledge_content_sha256

    connection.execute(
        "DELETE FROM knowledge_generation_dossier_plans WHERE generation_id = ?",
        (generation_id,),
    )
    rows = connection.execute(
        "SELECT identity_id, item_key, title FROM knowledge_generation_items "
        "WHERE generation_id = ? AND kind = 'entity' ORDER BY item_key",
        (generation_id,),
    ).fetchall()
    known_identity_ids = frozenset(
        str(value[0])
        for value in connection.execute(
            "SELECT identity_id FROM knowledge_generation_items "
            "WHERE generation_id = ? AND identity_id IS NOT NULL",
            (generation_id,),
        )
    )
    issues: list[str] = []
    for row in rows:
        identity_id = str(row[0]) if row[0] is not None else ""
        item_key = str(row[1])
        claims = dossier_claims_for_identity_in(connection, generation_id, identity_id)
        dossier_operation = planning_operation
        dossier_prompt_digest = prompt_contract_digest
        dossier_provenance = planner_provenance_json
        try:
            if planned is not None and identity_id in planned:
                plan = planned[identity_id]
            elif planner is not None:
                planned_dossier = planner(
                    document_name=str(row[2]),
                    generation_id=generation_id,
                    identity_id=identity_id,
                    claims=claims,
                    language=language,
                    known_related_identity_ids=known_identity_ids - {identity_id},
                )
                plan = planned_dossier.plan
                dossier_operation = planned_dossier.planning_operation
                dossier_prompt_digest = planned_dossier.prompt_contract_digest
                dossier_provenance = planned_dossier.planner_provenance_json
            else:
                plan = plan_entity_dossier(
                    generation_id=generation_id,
                    identity_id=identity_id,
                    claims=claims,
                    language=language,
                )
            validate_entity_dossier_plan(plan, claims)
            rendered = render_entity_dossier(plan, claims, language=language)
            if not rendered.markdown:
                raise DossierPlanValidationError(("empty_rendered_dossier",))
        except (DossierPlanValidationError, KeyError) as error:
            details = (
                error.issues if isinstance(error, DossierPlanValidationError) else ("missing_plan",)
            )
            issues.extend(f"dossier:{identity_id}:{detail}" for detail in details)
            continue
        _persist_dossier_in(
            connection,
            plan=plan,
            claims=claims,
            rendered_content_digest=rendered.content_digest,
            fact_count=rendered.fact_count,
            planning_operation=dossier_operation,
            prompt_contract_digest=dossier_prompt_digest,
            planner_provenance_json=dossier_provenance,
            language=language,
            now=now,
        )
        connection.execute(
            "UPDATE knowledge_generation_items "
            "SET content_markdown = ?, content_sha256 = ? "
            "WHERE generation_id = ? AND item_key = ?",
            (
                rendered.markdown,
                knowledge_content_sha256(rendered.markdown),
                generation_id,
                item_key,
            ),
        )
    connection.execute(
        "UPDATE knowledge_generation_manifests SET dossier_state = ?, updated_at = ? "
        "WHERE generation_id = ?",
        ("failed" if issues else "ready", now, generation_id),
    )
    return tuple(dict.fromkeys(issues))


def dossier_claims_for_identity_in(
    connection: sqlite3.Connection,
    generation_id: int,
    identity_id: str,
) -> tuple[DossierClaimSnapshot, ...]:
    """Load only claims mapped from the generation's pinned candidate inputs."""
    rows = connection.execute(
        """
        SELECT mappings.candidate_generation_id, mappings.candidate_id,
            claims.claim_ordinal, claims.role, claims.claim_text,
            claims.applicability_json, sources.evidence_id
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
    grouped: dict[tuple[str, str, int], list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row[0]), str(row[1]), int(row[2]))].append(row)
    claims: list[DossierClaimSnapshot] = []
    for (candidate_generation_id, candidate_id, claim_ordinal), values in grouped.items():
        applicability = _applicability(str(values[0][5]))
        claims.append(
            DossierClaimSnapshot(
                generation_id=generation_id,
                candidate_generation_id=candidate_generation_id,
                candidate_id=candidate_id,
                claim_ordinal=claim_ordinal,
                claim_id=candidate_claim_id(candidate_generation_id, candidate_id, claim_ordinal),
                identity_id=identity_id,
                text=str(values[0][4]),
                role=str(values[0][3]),
                applicability=applicability,
                evidence_ids=tuple(dict.fromkeys(str(value[6]) for value in values)),
            )
        )
    return tuple(claims)


def generation_entity_dossiers_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[PublishedEntityDossier, ...]:
    rows = connection.execute(
        """
        SELECT identity_id, status, plan_digest, summary_claim_ids_json,
            related_identity_ids_json, rendered_content_digest, fact_count, language
        FROM knowledge_generation_dossier_plans
        WHERE generation_id = ? ORDER BY identity_id
        """,
        (generation_id,),
    ).fetchall()
    result: list[PublishedEntityDossier] = []
    for row in rows:
        identity_id = str(row[0])
        plan = _plan_in(
            connection,
            generation_id,
            identity_id,
            summary_claim_ids_json=str(row[3]),
            related_identity_ids_json=str(row[4]),
        )
        result.append(
            PublishedEntityDossier(
                generation_id=generation_id,
                identity_id=identity_id,
                status=str(row[1]),
                plan=plan,
                plan_digest=str(row[2]),
                rendered_content_digest=str(row[5]),
                fact_count=int(row[6]),
                language=str(row[7]),  # type: ignore[arg-type]
            )
        )
    return tuple(result)


def generation_entity_dossier_issues_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[str, ...]:
    """Revalidate stored ownership, structure, rendering, and item content."""
    from openkb.desktop_knowledge_generations import knowledge_content_sha256

    expected = connection.execute(
        "SELECT COUNT(*) FROM knowledge_generation_items "
        "WHERE generation_id = ? AND kind = 'entity'",
        (generation_id,),
    ).fetchone()
    dossiers = generation_entity_dossiers_in(connection, generation_id)
    issues: list[str] = []
    if expected is not None and int(expected[0]) != len(dossiers):
        issues.append("dossier_incomplete")
    for dossier in dossiers:
        claims = dossier_claims_for_identity_in(connection, generation_id, dossier.identity_id)
        try:
            validate_entity_dossier_plan(dossier.plan, claims)
            rendered = render_entity_dossier(dossier.plan, claims, language=dossier.language)
        except DossierPlanValidationError as error:
            issues.extend(f"dossier:{dossier.identity_id}:{value}" for value in error.issues)
            continue
        if entity_dossier_plan_digest(dossier.plan) != dossier.plan_digest:
            issues.append(f"dossier:{dossier.identity_id}:plan_digest_mismatch")
        if rendered.content_digest != dossier.rendered_content_digest:
            issues.append(f"dossier:{dossier.identity_id}:render_digest_mismatch")
        if rendered.fact_count != dossier.fact_count:
            issues.append(f"dossier:{dossier.identity_id}:fact_count_mismatch")
        item = connection.execute(
            "SELECT content_markdown, content_sha256 FROM knowledge_generation_items "
            "WHERE generation_id = ? AND identity_id = ? AND kind = 'entity'",
            (generation_id, dossier.identity_id),
        ).fetchone()
        if (
            item is None
            or str(item[0]) != rendered.markdown
            or str(item[1]) != knowledge_content_sha256(rendered.markdown)
        ):
            issues.append(f"dossier:{dossier.identity_id}:rendered_item_mismatch")
    return tuple(dict.fromkeys(issues))


def _persist_dossier_in(
    connection: sqlite3.Connection,
    *,
    plan: EntityDossierPlan,
    claims: tuple[DossierClaimSnapshot, ...],
    rendered_content_digest: str,
    fact_count: int,
    planning_operation: str,
    prompt_contract_digest: str,
    planner_provenance_json: str,
    language: Literal["en", "zh"],
    now: str,
) -> None:
    plan_digest = entity_dossier_plan_digest(plan)
    connection.execute(
        """
        INSERT INTO knowledge_generation_dossier_plans (
            generation_id, identity_id, status, plan_digest, planning_operation,
            prompt_contract_digest, planner_provenance_json,
            language,
            summary_claim_ids_json, related_identity_ids_json,
            rendered_content_digest, fact_count, created_at
        ) VALUES (?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan.generation_id,
            plan.identity_id,
            plan_digest,
            planning_operation,
            prompt_contract_digest,
            planner_provenance_json,
            language,
            _json(plan.summary_claim_ids),
            _json(plan.related_identity_ids),
            rendered_content_digest,
            fact_count,
            now,
        ),
    )
    claims_by_id = {claim.claim_id: claim for claim in claims}
    display_ordinal = 0
    for claim_id in plan.summary_claim_ids:
        _persist_placement_in(
            connection,
            plan,
            claims_by_id[claim_id],
            placement_kind="summary",
            section_ordinal=None,
            unit_ordinal=None,
            display_ordinal=display_ordinal,
        )
        display_ordinal += 1
    for section_ordinal, section in enumerate(plan.sections):
        connection.execute(
            "INSERT INTO knowledge_generation_dossier_sections "
            "(generation_id, identity_id, section_ordinal, title, purpose) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                plan.generation_id,
                plan.identity_id,
                section_ordinal,
                section.title,
                section.purpose,
            ),
        )
        for unit_ordinal, unit in enumerate(section.units):
            connection.execute(
                "INSERT INTO knowledge_generation_dossier_units "
                "(generation_id, identity_id, section_ordinal, unit_ordinal, "
                "presentation, claim_ids_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan.generation_id,
                    plan.identity_id,
                    section_ordinal,
                    unit_ordinal,
                    unit.presentation,
                    _json(unit.claim_ids),
                ),
            )
            for claim_id in unit.claim_ids:
                _persist_placement_in(
                    connection,
                    plan,
                    claims_by_id[claim_id],
                    placement_kind="unit",
                    section_ordinal=section_ordinal,
                    unit_ordinal=unit_ordinal,
                    display_ordinal=display_ordinal,
                )
                display_ordinal += 1


def _persist_placement_in(
    connection: sqlite3.Connection,
    plan: EntityDossierPlan,
    claim: DossierClaimSnapshot,
    *,
    placement_kind: Literal["summary", "unit"],
    section_ordinal: int | None,
    unit_ordinal: int | None,
    display_ordinal: int,
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_generation_dossier_claim_placements (
            generation_id, identity_id, claim_id, candidate_generation_id,
            candidate_id, claim_ordinal, placement_kind, section_ordinal,
            unit_ordinal, display_ordinal
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan.generation_id,
            plan.identity_id,
            claim.claim_id,
            claim.candidate_generation_id,
            claim.candidate_id,
            claim.claim_ordinal,
            placement_kind,
            section_ordinal,
            unit_ordinal,
            display_ordinal,
        ),
    )


def _plan_in(
    connection: sqlite3.Connection,
    generation_id: int,
    identity_id: str,
    *,
    summary_claim_ids_json: str,
    related_identity_ids_json: str,
) -> EntityDossierPlan:
    section_rows = connection.execute(
        "SELECT section_ordinal, title, purpose FROM knowledge_generation_dossier_sections "
        "WHERE generation_id = ? AND identity_id = ? ORDER BY section_ordinal",
        (generation_id, identity_id),
    ).fetchall()
    sections: list[EntityDossierSection] = []
    for section_row in section_rows:
        section_ordinal = int(section_row[0])
        unit_rows = connection.execute(
            "SELECT presentation, claim_ids_json "
            "FROM knowledge_generation_dossier_units "
            "WHERE generation_id = ? AND identity_id = ? AND section_ordinal = ? "
            "ORDER BY unit_ordinal",
            (generation_id, identity_id, section_ordinal),
        ).fetchall()
        sections.append(
            EntityDossierSection(
                title=str(section_row[1]),
                purpose=str(section_row[2]),  # type: ignore[arg-type]
                units=tuple(
                    EntityDossierUnit(
                        presentation=str(unit[0]),  # type: ignore[arg-type]
                        claim_ids=_strings(str(unit[1])),
                    )
                    for unit in unit_rows
                ),
            )
        )
    return EntityDossierPlan(
        generation_id=generation_id,
        identity_id=identity_id,
        summary_claim_ids=_strings(summary_claim_ids_json),
        sections=tuple(sections),
        related_identity_ids=_strings(related_identity_ids_json),
    )


def _applicability(value: str) -> tuple[tuple[str, str], ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
    ):
        raise DossierPlanValidationError(("invalid_claim_applicability",))
    return tuple((str(key), str(item)) for key, item in sorted(parsed.items()))


def _strings(value: str) -> tuple[str, ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise DossierPlanValidationError(("invalid_persisted_identifiers",))
    return tuple(parsed)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
