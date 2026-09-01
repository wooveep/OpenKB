"""Versioned, evidence-bound Knowledge Analysis for Desktop imports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_knowledge_candidate_admission import assess_knowledge_candidate
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationSource,
    knowledge_content_sha256,
)
from openkb.desktop_knowledge_reconciliation_changes import IncomingKnowledgeChange
from openkb.desktop_knowledge_rendering import (
    RenderedKnowledgeClaim,
    render_generated_knowledge,
)
from openkb.desktop_knowledge_sources import stable_source_id
from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_prompt_contracts import (
    KNOWLEDGE_ANALYSIS_MAX_EVIDENCE_IDS_PER_CLAIM,
    prompt_contract_for,
)

KNOWLEDGE_ANALYSIS_SCHEMA_VERSION = "openkb.knowledge-analysis.v1"
KNOWLEDGE_ANALYSIS_SCOPE: Literal["document"] = "document"
KNOWLEDGE_ANALYSIS_BATCH_SCOPE: Literal["batch"] = "batch"
KnowledgeAnalysisScope = Literal["document", "batch"]
KnowledgeAnalysisKind = Literal["concept", "entity", "procedure"]
KnowledgeClaimRole = Literal[
    "definition",
    "purpose",
    "mechanism",
    "capability",
    "scope",
    "prerequisite",
    "step",
    "validation",
    "rollback",
    "troubleshooting",
    "limitation",
    "relation",
    "detail",
]
DocumentSummaryRole = Literal["purpose", "applicability", "key_topic"]
_KNOWLEDGE_ANALYSIS_CONTRACT = prompt_contract_for("knowledge_analysis")
KNOWLEDGE_ANALYSIS_SYSTEM_PROMPT = _KNOWLEDGE_ANALYSIS_CONTRACT.instructions
KNOWLEDGE_ANALYSIS_PROMPT_DIGEST = _KNOWLEDGE_ANALYSIS_CONTRACT.digest

_MAX_DESCRIPTION_CHARACTERS = 4_000
_MAX_CANDIDATES_PER_KIND = 32
_MAX_TITLE_CHARACTERS = 240
_MAX_ALIAS_OR_TAG_COUNT = 32
_MAX_ALIAS_OR_TAG_CHARACTERS = 160
_MAX_CLAIMS_PER_CANDIDATE = 64
_MAX_CLAIM_CHARACTERS = 4_000
_MAX_EVIDENCE_PER_CLAIM = KNOWLEDGE_ANALYSIS_MAX_EVIDENCE_IDS_PER_CLAIM
_MAX_AGGREGATE_CANDIDATES_PER_KIND = 4_096
_MAX_AGGREGATE_CLAIMS_PER_CANDIDATE = 4_096
_MAX_AGGREGATE_EVIDENCE_PER_CLAIM = 4_096
_MAX_EVIDENCE_TEXT_CHARACTERS = 12_000
_MAX_SUMMARY_UNITS = 32
_MAX_AGGREGATE_SUMMARY_UNITS = 4_096
_CLAIM_ROLES = frozenset(
    {
        "definition",
        "purpose",
        "mechanism",
        "capability",
        "scope",
        "prerequisite",
        "step",
        "validation",
        "rollback",
        "troubleshooting",
        "limitation",
        "relation",
        "detail",
    }
)
_SUMMARY_ROLES = frozenset({"purpose", "applicability", "key_topic"})


@dataclass(frozen=True)
class KnowledgeClaimApplicability:
    product_version: str | None = None
    platform: str | None = None
    deployment_scenario: str | None = None
    time_boundary: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "product_version": self.product_version or "",
            "platform": self.platform or "",
            "deployment_scenario": self.deployment_scenario or "",
            "time_boundary": self.time_boundary or "",
        }

    def values(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (dimension, value)
            for dimension, value in (
                ("product_version", self.product_version),
                ("platform", self.platform),
                ("deployment_scenario", self.deployment_scenario),
                ("time_boundary", self.time_boundary),
            )
            if value is not None
        )


@dataclass(frozen=True)
class KnowledgeAnalysisClaim:
    """One factual claim and the Evidence IDs the provider says support it."""

    text: str
    source_evidence_ids: tuple[str, ...]
    role: KnowledgeClaimRole = "detail"
    applicability: KnowledgeClaimApplicability = KnowledgeClaimApplicability()

    def as_dict(self, *, extended: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "text": self.text,
            "source_evidence_ids": list(self.source_evidence_ids),
        }
        if extended:
            payload["role"] = self.role
            payload["applicability"] = self.applicability.as_dict()
        return payload


@dataclass(frozen=True)
class KnowledgeAnalysisSummaryUnit:
    role: DocumentSummaryRole
    text: str
    source_evidence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "text": self.text,
            "source_evidence_ids": list(self.source_evidence_ids),
        }


def bounded_document_summary_units(
    units: tuple[KnowledgeAnalysisSummaryUnit, ...],
    *,
    maximum: int = _MAX_SUMMARY_UNITS,
) -> tuple[KnowledgeAnalysisSummaryUnit, ...]:
    """Retain role and whole-document coverage under the published summary bound."""
    if maximum < 0:
        raise ValueError("Document Summary maximum must not be negative.")
    if len(units) <= maximum:
        return units
    if maximum == 0:
        return ()
    selected: set[int] = set()
    for role in ("purpose", "applicability", "key_topic"):
        match = next((index for index, unit in enumerate(units) if unit.role == role), None)
        if match is not None and len(selected) < maximum:
            selected.add(match)
    coverage_slots = maximum - len(selected)
    if coverage_slots == 1:
        selected.add(len(units) // 2)
    elif coverage_slots > 1:
        for ordinal in range(coverage_slots):
            selected.add(round(ordinal * (len(units) - 1) / (coverage_slots - 1)))
    if len(selected) < maximum:
        for index in range(len(units)):
            selected.add(index)
            if len(selected) == maximum:
                break
    return tuple(units[index] for index in sorted(selected))


@dataclass(frozen=True)
class KnowledgeAnalysisCandidate:
    """One normalized Concept, Entity, or Procedure candidate from model output."""

    kind: KnowledgeAnalysisKind
    title: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    claims: tuple[KnowledgeAnalysisClaim, ...]
    subtype: str | None = None

    def as_dict(self, *, extended: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "title": self.title,
            "aliases": list(self.aliases),
            "tags": list(self.tags),
            "claims": [claim.as_dict(extended=extended) for claim in self.claims],
        }
        if self.kind == "entity" and self.subtype is not None:
            payload["subtype"] = self.subtype
        return payload


@dataclass(frozen=True)
class KnowledgeAnalysisMissingClaim:
    """One valid model claim that cannot yet resolve every declared source."""

    kind: KnowledgeAnalysisKind
    title: str
    normalized_title: str
    entity_subtype: str | None
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    claim_text: str
    source_evidence_ids: tuple[str, ...]
    reason: Literal["source_not_provided", "source_reference_unresolved"]


@dataclass(frozen=True)
class DesktopKnowledgeAnalysis:
    """The validated document-level result retained in the Stage checkpoint."""

    document_description: str
    concepts: tuple[KnowledgeAnalysisCandidate, ...]
    entities: tuple[KnowledgeAnalysisCandidate, ...]
    analysis_scope: KnowledgeAnalysisScope = KNOWLEDGE_ANALYSIS_SCOPE
    procedures: tuple[KnowledgeAnalysisCandidate, ...] = ()
    document_summary: tuple[KnowledgeAnalysisSummaryUnit, ...] = ()
    corpus_ready: bool = False

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": self.analysis_scope,
            "document_description": self.document_description,
            "concepts": [
                candidate.as_dict(extended=self.corpus_ready) for candidate in self.concepts
            ],
            "entities": [
                candidate.as_dict(extended=self.corpus_ready) for candidate in self.entities
            ],
        }
        if self.corpus_ready:
            payload["document_summary"] = [unit.as_dict() for unit in self.document_summary]
            payload["procedures"] = [
                candidate.as_dict(extended=True) for candidate in self.procedures
            ]
        return payload

    @property
    def candidates(self) -> tuple[KnowledgeAnalysisCandidate, ...]:
        return (*self.concepts, *self.entities, *self.procedures)

    def incoming_changes(
        self,
        evidence_id_map: Mapping[str, str],
        *,
        analysis_provenance_json: str,
    ) -> tuple[IncomingKnowledgeChange, ...]:
        """Return only fully resolvable claims; unresolved claims remain in the checkpoint."""
        changes: list[IncomingKnowledgeChange] = []
        for candidate in self.candidates:
            resolved_claims = tuple(
                claim
                for claim in candidate.claims
                if claim.source_evidence_ids
                and all(evidence_id in evidence_id_map for evidence_id in claim.source_evidence_ids)
            )
            if not resolved_claims:
                continue
            if self.corpus_ready:
                admission = assess_knowledge_candidate(
                    kind=candidate.kind,
                    title=candidate.title,
                    subtype=candidate.subtype,
                    claims=tuple((claim.role, claim.text) for claim in resolved_claims),
                )
                if not admission.admitted:
                    continue
            title, normalized_title = normalize_knowledge_title(candidate.title)
            if not title:
                continue
            sources: list[KnowledgeGenerationSource] = []
            rendered_claims: list[RenderedKnowledgeClaim] = []
            legacy_body: list[str] = []
            for claim in resolved_claims:
                markers: list[str] = []
                canonical_evidence_ids: set[str] = set()
                for evidence_id in claim.source_evidence_ids:
                    canonical_evidence_id = evidence_id_map[evidence_id]
                    if canonical_evidence_id in canonical_evidence_ids:
                        continue
                    canonical_evidence_ids.add(canonical_evidence_id)
                    source_id = stable_source_id(canonical_evidence_id)
                    sources.append(
                        KnowledgeGenerationSource(
                            source_id=source_id,
                            evidence_id=canonical_evidence_id,
                            claim_text=claim.text,
                        )
                    )
                    markers.append(f"[^{source_id}]")
                rendered_claims.append(
                    RenderedKnowledgeClaim(
                        text=claim.text,
                        role=claim.role,
                        source_markers=tuple(markers),
                        applicability=claim.applicability.values(),
                    )
                )
                legacy_body.append(f"{claim.text}{''.join(markers)}")
            content_markdown = (
                render_generated_knowledge(candidate.kind, tuple(rendered_claims))
                if self.corpus_ready
                else "\n\n".join(legacy_body)
            )
            changes.append(
                IncomingKnowledgeChange(
                    source_block_id=None,
                    kind=candidate.kind,
                    is_kind_explicit=True,
                    title=title,
                    normalized_title=normalized_title,
                    content_markdown=content_markdown,
                    content_sha256=knowledge_content_sha256(content_markdown),
                    entity_subtype=candidate.subtype,
                    aliases=candidate.aliases,
                    tags=candidate.tags,
                    sources=tuple(sources),
                    analysis_provenance_json=analysis_provenance_json,
                )
            )
        return tuple(changes)

    def missing_source_claims(
        self, evidence_id_map: Mapping[str, str]
    ) -> tuple[KnowledgeAnalysisMissingClaim, ...]:
        """Return claim-level review work without rejecting valid sibling claims."""
        missing: list[KnowledgeAnalysisMissingClaim] = []
        for candidate in self.candidates:
            title, normalized_title = normalize_knowledge_title(candidate.title)
            if not title:
                continue
            for claim in candidate.claims:
                if claim.source_evidence_ids and all(
                    evidence_id in evidence_id_map for evidence_id in claim.source_evidence_ids
                ):
                    continue
                missing.append(
                    KnowledgeAnalysisMissingClaim(
                        kind=candidate.kind,
                        title=title,
                        normalized_title=normalized_title,
                        entity_subtype=candidate.subtype,
                        aliases=candidate.aliases,
                        tags=candidate.tags,
                        claim_text=claim.text,
                        source_evidence_ids=claim.source_evidence_ids,
                        reason=(
                            "source_reference_unresolved"
                            if claim.source_evidence_ids
                            else "source_not_provided"
                        ),
                    )
                )
        return tuple(missing)


def parse_knowledge_analysis(
    content: str,
    *,
    expected_scope: KnowledgeAnalysisScope = KNOWLEDGE_ANALYSIS_SCOPE,
    aggregate: bool = False,
) -> DesktopKnowledgeAnalysis:
    """Parse and strictly validate one provider response without retrying it."""
    try:
        payload = json.loads(_json_object_text(content))
    except (json.JSONDecodeError, TypeError) as error:
        raise _invalid_response("Knowledge Analysis did not return valid JSON.") from error
    if not isinstance(payload, dict):
        raise _invalid_response("Knowledge Analysis must return one JSON object.")
    base_fields = {
        "schema_version",
        "analysis_scope",
        "document_description",
        "concepts",
        "entities",
    }
    extended_fields = {"document_summary", "procedures"}
    fields = frozenset(payload)
    if fields not in {frozenset(base_fields), frozenset(base_fields | extended_fields)}:
        raise _invalid_response("Knowledge Analysis returned an unsupported response shape.")
    if payload.get("schema_version") != KNOWLEDGE_ANALYSIS_SCHEMA_VERSION:
        raise _invalid_response("Knowledge Analysis returned an unsupported schema version.")
    if payload.get("analysis_scope") != expected_scope:
        raise _invalid_response(f"Knowledge Analysis must use {expected_scope} scope.")
    description = _string(
        payload.get("document_description"),
        "document_description",
        maximum=_MAX_DESCRIPTION_CHARACTERS,
        allow_empty=True,
    )
    maximum_candidates = (
        _MAX_AGGREGATE_CANDIDATES_PER_KIND if aggregate else _MAX_CANDIDATES_PER_KIND
    )
    maximum_claims = _MAX_AGGREGATE_CLAIMS_PER_CANDIDATE if aggregate else _MAX_CLAIMS_PER_CANDIDATE
    maximum_sources = _MAX_AGGREGATE_EVIDENCE_PER_CLAIM if aggregate else _MAX_EVIDENCE_PER_CLAIM
    corpus_ready = extended_fields <= fields
    concepts = _candidates(
        payload.get("concepts"),
        "concept",
        maximum_candidates=maximum_candidates,
        maximum_claims=maximum_claims,
        maximum_sources=maximum_sources,
        extended=corpus_ready,
    )
    entities = _candidates(
        payload.get("entities"),
        "entity",
        maximum_candidates=maximum_candidates,
        maximum_claims=maximum_claims,
        maximum_sources=maximum_sources,
        extended=corpus_ready,
    )
    procedures = (
        _candidates(
            payload.get("procedures"),
            "procedure",
            maximum_candidates=maximum_candidates,
            maximum_claims=maximum_claims,
            maximum_sources=maximum_sources,
            extended=True,
        )
        if corpus_ready
        else ()
    )
    document_summary: tuple[KnowledgeAnalysisSummaryUnit, ...] = ()
    if corpus_ready:
        document_summary = bounded_document_summary_units(
            _summary_units(
                payload.get("document_summary"),
                maximum_sources=maximum_sources,
                maximum_units=(_MAX_AGGREGATE_SUMMARY_UNITS if aggregate else _MAX_SUMMARY_UNITS),
            )
        )
    identities = [
        (item.kind, normalize_knowledge_title(item.title)[1])
        for item in (*concepts, *entities, *procedures)
    ]
    if len(identities) != len(set(identities)):
        raise _invalid_response("Knowledge Analysis returned duplicate candidate identities.")
    return DesktopKnowledgeAnalysis(
        description,
        concepts,
        entities,
        expected_scope,
        procedures,
        document_summary,
        corpus_ready,
    )


def knowledge_analysis_from_checkpoint(payload: object) -> DesktopKnowledgeAnalysis | None:
    """Restore a new structured checkpoint while leaving legacy summaries compatible."""
    if not isinstance(payload, dict):
        return None
    normalized = payload.get("normalized_result")
    if not isinstance(normalized, dict):
        return None
    return parse_knowledge_analysis(
        json.dumps(normalized, ensure_ascii=False), aggregate="batch_count" in payload
    )


def knowledge_analysis_provenance_json(
    *,
    provider: str,
    model: str,
    prompt_digest: str,
    engine_version: str,
    schema_version: str = KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
) -> str:
    """Serialize non-secret producer identity for SQLite and OKF projection."""
    return json.dumps(
        {
            "schema_version": schema_version,
            "provider": provider,
            "model": model,
            "prompt_digest": prompt_digest,
            "engine_version": engine_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def knowledge_analysis_provenance_from_checkpoint(payload: object) -> str:
    if not isinstance(payload, dict):
        raise DesktopImportError(
            "import_checkpoint_invalid", "Knowledge Analysis checkpoint is invalid."
        )
    fields = ("provider", "model", "engine_version")
    values = tuple(payload.get(field) for field in fields)
    prompt_digest = payload.get("analysis_prompt_digest", payload.get("prompt_digest"))
    normalized = payload.get("normalized_result")
    schema_version = normalized.get("schema_version") if isinstance(normalized, dict) else None
    if not all(
        isinstance(value, str) and value for value in (*values, prompt_digest, schema_version)
    ):
        raise DesktopImportError(
            "import_checkpoint_invalid", "Knowledge Analysis provenance is invalid."
        )
    return knowledge_analysis_provenance_json(
        provider=str(values[0]),
        model=str(values[1]),
        prompt_digest=str(prompt_digest),
        engine_version=str(values[2]),
        schema_version=str(schema_version),
    )


def knowledge_analysis_prompt(
    document_name: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> str:
    """Build the non-secret model input with stable Evidence IDs and source locators."""
    payload = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": KNOWLEDGE_ANALYSIS_SCOPE,
        "document_name": Path(document_name).name,
        "evidence": [
            {
                "evidence_id": evidence_id,
                "kind": block.kind,
                "section": " / ".join(block.heading_path),
                "locator": block.locator
                or {"line_start": block.line_start, "line_end": block.line_end},
                "text": block.text[:_MAX_EVIDENCE_TEXT_CHARACTERS],
            }
            for evidence_id, block in evidence
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _candidates(
    value: object,
    kind: KnowledgeAnalysisKind,
    *,
    maximum_candidates: int,
    maximum_claims: int,
    maximum_sources: int,
    extended: bool,
) -> tuple[KnowledgeAnalysisCandidate, ...]:
    if not isinstance(value, list) or _exceeds_limit(value, maximum_candidates):
        raise _invalid_response(f"Knowledge Analysis {kind} candidates are invalid.")
    candidates: list[KnowledgeAnalysisCandidate] = []
    for item in value:
        if not isinstance(item, dict):
            raise _invalid_response(f"Knowledge Analysis {kind} candidate is invalid.")
        allowed = {"title", "aliases", "tags", "claims"}
        if kind == "entity":
            allowed.add("subtype")
        if set(item) - allowed or not {"title", "aliases", "tags", "claims"} <= set(item):
            raise _invalid_response(
                f"Knowledge Analysis {kind} candidate fields are invalid; "
                "put source_evidence_ids only inside claims."
            )
        title = _string(item.get("title"), "title", maximum=_MAX_TITLE_CHARACTERS)
        aliases = _string_list(item.get("aliases"), "aliases")
        tags = _string_list(item.get("tags"), "tags")
        claims_value = item.get("claims")
        if not isinstance(claims_value, list) or _exceeds_limit(claims_value, maximum_claims):
            raise _invalid_response("Knowledge Analysis claims are invalid.")
        unique_claims: list[KnowledgeAnalysisClaim] = []
        claim_indexes: dict[str, int] = {}
        for claim_value in claims_value:
            claim = _claim(
                claim_value,
                maximum_sources=maximum_sources,
                extended=extended,
            )
            claim_key = _claim_identity(claim)
            existing_index = claim_indexes.get(claim_key)
            if existing_index is None:
                claim_indexes[claim_key] = len(unique_claims)
                unique_claims.append(claim)
                continue
            existing = unique_claims[existing_index]
            source_ids = tuple(
                dict.fromkeys((*existing.source_evidence_ids, *claim.source_evidence_ids))
            )
            if len(source_ids) > maximum_sources:
                raise _claim_source_limit_error(maximum_sources)
            unique_claims[existing_index] = KnowledgeAnalysisClaim(
                existing.text,
                source_ids,
                existing.role,
                existing.applicability,
            )
        claims = tuple(unique_claims)
        subtype_value = item.get("subtype")
        subtype = (
            None
            if subtype_value is None
            else _string(subtype_value, "subtype", maximum=_MAX_ALIAS_OR_TAG_CHARACTERS)
        )
        candidates.append(KnowledgeAnalysisCandidate(kind, title, aliases, tags, claims, subtype))
    return tuple(candidates)


def _claim(value: object, *, maximum_sources: int, extended: bool) -> KnowledgeAnalysisClaim:
    required = {"text", "source_evidence_ids", "role", "applicability"}
    legacy = {"text", "source_evidence_ids"}
    if not isinstance(value, dict) or set(value) != (required if extended else legacy):
        raise _invalid_response("Knowledge Analysis claim is invalid.")
    text = _string(value.get("text"), "claim text", maximum=_MAX_CLAIM_CHARACTERS)
    source_values = value.get("source_evidence_ids")
    if not isinstance(source_values, list):
        raise _invalid_response("Knowledge Analysis claim sources are invalid.")
    if _exceeds_limit(source_values, maximum_sources):
        raise _claim_source_limit_error(maximum_sources)
    source_ids: list[str] = []
    for source in source_values:
        evidence_id = _string(source, "evidence ID", maximum=160)
        if evidence_id not in source_ids:
            source_ids.append(evidence_id)
    role: KnowledgeClaimRole = "detail"
    applicability = KnowledgeClaimApplicability()
    if extended:
        role_value = value.get("role")
        if not isinstance(role_value, str) or role_value not in _CLAIM_ROLES:
            raise _invalid_response("Knowledge Analysis claim role is invalid.")
        role = cast(KnowledgeClaimRole, role_value)
        applicability = _applicability(value.get("applicability"))
    return KnowledgeAnalysisClaim(text, tuple(source_ids), role, applicability)


def _summary_units(
    value: object, *, maximum_sources: int, maximum_units: int
) -> tuple[KnowledgeAnalysisSummaryUnit, ...]:
    if not isinstance(value, list) or len(value) > maximum_units:
        raise _invalid_response("Knowledge Analysis document summary is invalid.")
    units: list[KnowledgeAnalysisSummaryUnit] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "role",
            "text",
            "source_evidence_ids",
        }:
            raise _invalid_response("Knowledge Analysis document summary unit is invalid.")
        role = item.get("role")
        if not isinstance(role, str) or role not in _SUMMARY_ROLES:
            raise _invalid_response("Knowledge Analysis document summary role is invalid.")
        claim = _claim(
            {
                "text": item.get("text"),
                "source_evidence_ids": item.get("source_evidence_ids"),
            },
            maximum_sources=maximum_sources,
            extended=False,
        )
        units.append(
            KnowledgeAnalysisSummaryUnit(
                cast(DocumentSummaryRole, role),
                claim.text,
                claim.source_evidence_ids,
            )
        )
    return tuple(units)


def _applicability(value: object) -> KnowledgeClaimApplicability:
    fields = (
        "product_version",
        "platform",
        "deployment_scenario",
        "time_boundary",
    )
    if not isinstance(value, dict) or set(value) != set(fields):
        raise _invalid_response("Knowledge Analysis claim applicability is invalid.")
    values: list[str | None] = []
    for field in fields:
        normalized = _string(
            value.get(field),
            f"claim applicability {field}",
            maximum=_MAX_ALIAS_OR_TAG_CHARACTERS,
            allow_empty=True,
        )
        values.append(normalized or None)
    return KnowledgeClaimApplicability(*values)


def _claim_identity(claim: KnowledgeAnalysisClaim) -> str:
    scope = "\x1f".join(
        (
            claim.applicability.product_version or "",
            claim.applicability.platform or "",
            claim.applicability.deployment_scenario or "",
            claim.applicability.time_boundary or "",
        )
    )
    return f"{claim.role}\x1f{scope}\x1f{' '.join(claim.text.split()).casefold()}"


def _claim_source_limit_error(maximum_sources: int) -> DesktopImportError:
    return _invalid_response(
        f"Knowledge Analysis claim must reference at most {maximum_sources} supplied Evidence IDs."
    )


def _exceeds_limit(value: list[object], maximum: int) -> bool:
    return len(value) > maximum


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ALIAS_OR_TAG_COUNT:
        raise _invalid_response(f"Knowledge Analysis {field} are invalid.")
    result: list[str] = []
    for item in value:
        normalized = _string(item, field, maximum=_MAX_ALIAS_OR_TAG_CHARACTERS)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _string(value: object, field: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _invalid_response(f"Knowledge Analysis {field} is invalid.")
    normalized = " ".join(value.split())
    if (not normalized and not allow_empty) or len(normalized) > maximum:
        raise _invalid_response(f"Knowledge Analysis {field} is invalid.")
    return normalized


def _json_object_text(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0].strip()
    return stripped


def _invalid_response(message: str) -> DesktopImportError:
    return DesktopImportError(
        "model_response_invalid",
        message,
        suggested_action="Choose a model that can return the required Knowledge Analysis JSON.",
    )
