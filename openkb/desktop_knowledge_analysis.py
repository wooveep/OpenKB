"""Versioned, evidence-bound Knowledge Analysis for Desktop imports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationSource,
    knowledge_content_sha256,
)
from openkb.desktop_knowledge_reconciliation_changes import IncomingKnowledgeChange
from openkb.desktop_knowledge_sources import stable_source_id
from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_prompt_contracts import prompt_contract_for

KNOWLEDGE_ANALYSIS_SCHEMA_VERSION = "openkb.knowledge-analysis.v1"
KNOWLEDGE_ANALYSIS_SCOPE: Literal["document"] = "document"
KNOWLEDGE_ANALYSIS_BATCH_SCOPE: Literal["batch"] = "batch"
KnowledgeAnalysisScope = Literal["document", "batch"]
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
_MAX_EVIDENCE_PER_CLAIM = 8
_MAX_AGGREGATE_CANDIDATES_PER_KIND = 4_096
_MAX_AGGREGATE_CLAIMS_PER_CANDIDATE = 4_096
_MAX_AGGREGATE_EVIDENCE_PER_CLAIM = 4_096
_MAX_EVIDENCE_TEXT_CHARACTERS = 12_000


@dataclass(frozen=True)
class KnowledgeAnalysisClaim:
    """One factual claim and the Evidence IDs the provider says support it."""

    text: str
    source_evidence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "source_evidence_ids": list(self.source_evidence_ids),
        }


@dataclass(frozen=True)
class KnowledgeAnalysisCandidate:
    """One normalized Concept or Entity candidate from the model response."""

    kind: Literal["concept", "entity"]
    title: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    claims: tuple[KnowledgeAnalysisClaim, ...]
    subtype: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "title": self.title,
            "aliases": list(self.aliases),
            "tags": list(self.tags),
            "claims": [claim.as_dict() for claim in self.claims],
        }
        if self.kind == "entity" and self.subtype is not None:
            payload["subtype"] = self.subtype
        return payload


@dataclass(frozen=True)
class KnowledgeAnalysisMissingClaim:
    """One valid model claim that cannot yet resolve every declared source."""

    kind: Literal["concept", "entity"]
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

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": self.analysis_scope,
            "document_description": self.document_description,
            "concepts": [candidate.as_dict() for candidate in self.concepts],
            "entities": [candidate.as_dict() for candidate in self.entities],
        }

    def incoming_changes(
        self,
        evidence_id_map: Mapping[str, str],
        *,
        analysis_provenance_json: str,
    ) -> tuple[IncomingKnowledgeChange, ...]:
        """Return only fully resolvable claims; unresolved claims remain in the checkpoint."""
        changes: list[IncomingKnowledgeChange] = []
        for candidate in (*self.concepts, *self.entities):
            resolved_claims = tuple(
                claim
                for claim in candidate.claims
                if claim.source_evidence_ids
                and all(evidence_id in evidence_id_map for evidence_id in claim.source_evidence_ids)
            )
            if not resolved_claims:
                continue
            title, normalized_title = normalize_knowledge_title(candidate.title)
            if not title:
                continue
            sources: list[KnowledgeGenerationSource] = []
            body: list[str] = []
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
                body.append(f"{claim.text}{''.join(markers)}")
            content_markdown = "\n\n".join(body)
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
        for candidate in (*self.concepts, *self.entities):
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
    if set(payload) != {
        "schema_version",
        "analysis_scope",
        "document_description",
        "concepts",
        "entities",
    }:
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
    concepts = _candidates(
        payload.get("concepts"),
        "concept",
        maximum_candidates=maximum_candidates,
        maximum_claims=maximum_claims,
        maximum_sources=maximum_sources,
    )
    entities = _candidates(
        payload.get("entities"),
        "entity",
        maximum_candidates=maximum_candidates,
        maximum_claims=maximum_claims,
        maximum_sources=maximum_sources,
    )
    identities = [
        (item.kind, normalize_knowledge_title(item.title)[1]) for item in (*concepts, *entities)
    ]
    if len(identities) != len(set(identities)):
        raise _invalid_response("Knowledge Analysis returned duplicate candidate identities.")
    return DesktopKnowledgeAnalysis(description, concepts, entities, expected_scope)


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
    kind: Literal["concept", "entity"],
    *,
    maximum_candidates: int,
    maximum_claims: int,
    maximum_sources: int,
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
            raise _invalid_response(f"Knowledge Analysis {kind} candidate is invalid.")
        title = _string(item.get("title"), "title", maximum=_MAX_TITLE_CHARACTERS)
        aliases = _string_list(item.get("aliases"), "aliases")
        tags = _string_list(item.get("tags"), "tags")
        claims_value = item.get("claims")
        if not isinstance(claims_value, list) or _exceeds_limit(claims_value, maximum_claims):
            raise _invalid_response("Knowledge Analysis claims are invalid.")
        unique_claims: list[KnowledgeAnalysisClaim] = []
        claim_indexes: dict[str, int] = {}
        for claim_value in claims_value:
            claim = _claim(claim_value, maximum_sources=maximum_sources)
            existing_index = claim_indexes.get(claim.text)
            if existing_index is None:
                claim_indexes[claim.text] = len(unique_claims)
                unique_claims.append(claim)
                continue
            existing = unique_claims[existing_index]
            source_ids = tuple(
                dict.fromkeys((*existing.source_evidence_ids, *claim.source_evidence_ids))
            )
            if len(source_ids) > maximum_sources:
                raise _invalid_response("Knowledge Analysis claim sources are invalid.")
            unique_claims[existing_index] = KnowledgeAnalysisClaim(existing.text, source_ids)
        claims = tuple(unique_claims)
        subtype_value = item.get("subtype")
        subtype = (
            None
            if subtype_value is None
            else _string(subtype_value, "subtype", maximum=_MAX_ALIAS_OR_TAG_CHARACTERS)
        )
        candidates.append(KnowledgeAnalysisCandidate(kind, title, aliases, tags, claims, subtype))
    return tuple(candidates)


def _claim(value: object, *, maximum_sources: int) -> KnowledgeAnalysisClaim:
    if not isinstance(value, dict) or set(value) != {"text", "source_evidence_ids"}:
        raise _invalid_response("Knowledge Analysis claim is invalid.")
    text = _string(value.get("text"), "claim text", maximum=_MAX_CLAIM_CHARACTERS)
    source_values = value.get("source_evidence_ids")
    if not isinstance(source_values, list) or _exceeds_limit(source_values, maximum_sources):
        raise _invalid_response("Knowledge Analysis claim sources are invalid.")
    source_ids: list[str] = []
    for source in source_values:
        evidence_id = _string(source, "evidence ID", maximum=160)
        if evidence_id not in source_ids:
            source_ids.append(evidence_id)
    return KnowledgeAnalysisClaim(text, tuple(source_ids))


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
