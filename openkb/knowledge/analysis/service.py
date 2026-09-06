"""Versioned, evidence-bound Knowledge Analysis for Desktop imports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.documents.summary import bounded_document_summary_units
from openkb.importing.artifacts import DesktopImportError, DocumentIRBlock
from openkb.knowledge.analysis.validation import (
    KnowledgeAnalysisValidationError,
    validated_or_default,
)
from openkb.knowledge.analysis.validation import (
    exceeds_limit as _exceeds_limit,
)
from openkb.knowledge.analysis.validation import (
    invalid_response as _invalid_response,
)
from openkb.knowledge.analysis.validation import (
    json_object_text as _json_object_text,
)
from openkb.knowledge.pages.generations import (
    KnowledgeGenerationSource,
    knowledge_content_sha256,
)
from openkb.knowledge.pages.rendering import (
    RenderedKnowledgeClaim,
    render_generated_knowledge,
)
from openkb.knowledge.pages.sources import stable_source_id
from openkb.knowledge.pages.titles import normalize_knowledge_title
from openkb.knowledge.reconciliation.changes import IncomingKnowledgeChange
from openkb.models.prompt_contracts import (
    KNOWLEDGE_ANALYSIS_MAX_EVIDENCE_IDS_PER_CLAIM,
    prompt_contract_for,
)
from openkb.models.semantic_structure_contracts import normalize_dynamic_semantic_text

KNOWLEDGE_ANALYSIS_SCHEMA_VERSION = "openkb.knowledge-analysis.v2"
KNOWLEDGE_ANALYSIS_SCOPE: Literal["document"] = "document"
KNOWLEDGE_ANALYSIS_BATCH_SCOPE: Literal["batch"] = "batch"
KnowledgeAnalysisScope = Literal["document", "batch"]
KnowledgeAnalysisKind = Literal["concept", "entity", "procedure"]
KnowledgeCandidateAdmission = Literal["admit", "review", "exclude"]
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
_MAX_EVIDENCE_ID_CHARACTERS = 160
_MAX_AGGREGATE_CANDIDATES_PER_KIND = 4_096
_MAX_AGGREGATE_CLAIMS_PER_CANDIDATE = 4_096
_MAX_SUMMARY_UNITS = 32
_MAX_AGGREGATE_SUMMARY_UNITS = 4_096
_CANDIDATE_KINDS = frozenset({"concept", "entity", "procedure"})
_CANDIDATE_ADMISSIONS = frozenset({"admit", "review", "exclude"})


@dataclass(frozen=True)
class KnowledgeClaimApplicability:
    """One open applicability dimension backed by a subset of its claim Evidence."""

    dimension: str
    value: str
    source_evidence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "source_evidence_ids": list(self.source_evidence_ids),
        }


@dataclass(frozen=True)
class KnowledgeAnalysisClaim:
    """One factual claim and the Evidence IDs the provider says support it."""

    text: str
    source_evidence_ids: tuple[str, ...]
    applicability: tuple[KnowledgeClaimApplicability, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "source_evidence_ids": list(self.source_evidence_ids),
            "applicability": [entry.as_dict() for entry in self.applicability],
        }


@dataclass(frozen=True)
class KnowledgeAnalysisSummaryUnit:
    label: str
    text: str
    source_evidence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "text": self.text,
            "source_evidence_ids": list(self.source_evidence_ids),
        }


@dataclass(frozen=True)
class KnowledgeAnalysisCandidate:
    """One normalized Concept, Entity, or Procedure candidate from model output."""

    kind: KnowledgeAnalysisKind
    title: str
    aliases: tuple[str, ...]
    identity_labels: tuple[str, ...]
    claims: tuple[KnowledgeAnalysisClaim, ...]
    admission: KnowledgeCandidateAdmission = "admit"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "title": self.title,
            "aliases": list(self.aliases),
            "identity_labels": list(self.identity_labels),
            "admission": self.admission,
            "claims": [claim.as_dict() for claim in self.claims],
        }


@dataclass(frozen=True)
class KnowledgeAnalysisMissingClaim:
    """One valid model claim that cannot yet resolve every declared source."""

    kind: KnowledgeAnalysisKind
    title: str
    normalized_title: str
    aliases: tuple[str, ...]
    identity_labels: tuple[str, ...]
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

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": self.analysis_scope,
            "document_description": self.document_description,
            "document_summary": [unit.as_dict() for unit in self.document_summary],
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }

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
            if candidate.admission != "admit":
                continue
            title, normalized_title = normalize_knowledge_title(candidate.title)
            if not title:
                continue
            sources: list[KnowledgeGenerationSource] = []
            rendered_claims: list[RenderedKnowledgeClaim] = []
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
                        source_markers=tuple(markers),
                        applicability=tuple(
                            (entry.dimension, entry.value) for entry in claim.applicability
                        ),
                    )
                )
            content_markdown = render_generated_knowledge(candidate.kind, tuple(rendered_claims))
            changes.append(
                IncomingKnowledgeChange(
                    source_block_id=None,
                    kind=candidate.kind,
                    is_kind_explicit=True,
                    title=title,
                    normalized_title=normalized_title,
                    content_markdown=content_markdown,
                    content_sha256=knowledge_content_sha256(content_markdown),
                    aliases=candidate.aliases,
                    identity_labels=candidate.identity_labels,
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
                        aliases=candidate.aliases,
                        identity_labels=candidate.identity_labels,
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
    known_evidence_ids: frozenset[str] | None = None,
) -> DesktopKnowledgeAnalysis:
    """Parse and strictly validate one provider response without retrying it."""
    try:
        payload = json.loads(_json_object_text(content))
    except (json.JSONDecodeError, TypeError) as error:
        raise _invalid_response("Knowledge Analysis did not return valid JSON.") from error
    if not isinstance(payload, dict):
        raise _invalid_response("Knowledge Analysis must return one JSON object.")
    expected_fields = {
        "schema_version",
        "analysis_scope",
        "document_description",
        "document_summary",
        "candidates",
    }
    if set(payload) != expected_fields:
        raise _invalid_response("Knowledge Analysis returned an unsupported response shape.")
    if payload.get("schema_version") != KNOWLEDGE_ANALYSIS_SCHEMA_VERSION:
        raise _invalid_response("Knowledge Analysis returned an unsupported schema version.")
    if payload.get("analysis_scope") != expected_scope:
        raise _invalid_response(f"Knowledge Analysis must use {expected_scope} scope.")
    maximum_candidates = (
        _MAX_AGGREGATE_CANDIDATES_PER_KIND
        if aggregate
        else _MAX_CANDIDATES_PER_KIND * len(_CANDIDATE_KINDS)
    )
    maximum_claims = _MAX_AGGREGATE_CLAIMS_PER_CANDIDATE if aggregate else _MAX_CLAIMS_PER_CANDIDATE
    maximum_sources = _MAX_EVIDENCE_PER_CLAIM
    validation_errors: list[str] = []
    empty_candidates: tuple[KnowledgeAnalysisCandidate, ...] = ()

    description = validated_or_default(
        lambda: _string(
            payload.get("document_description"),
            "document_description",
            maximum=_MAX_DESCRIPTION_CHARACTERS,
            allow_empty=True,
        ),
        "",
        validation_errors,
    )

    all_candidates = validated_or_default(
        lambda: _candidates(
            payload.get("candidates"),
            maximum_candidates=maximum_candidates,
            maximum_claims=maximum_claims,
            maximum_sources=maximum_sources,
            known_evidence_ids=known_evidence_ids,
        ),
        empty_candidates,
        validation_errors,
    )
    empty_summary: tuple[KnowledgeAnalysisSummaryUnit, ...] = ()
    document_summary = validated_or_default(
        lambda: bounded_document_summary_units(
            _summary_units(
                payload.get("document_summary"),
                maximum_sources=maximum_sources,
                maximum_units=(_MAX_AGGREGATE_SUMMARY_UNITS if aggregate else _MAX_SUMMARY_UNITS),
                known_evidence_ids=known_evidence_ids,
            ),
            maximum=_MAX_SUMMARY_UNITS,
        ),
        empty_summary,
        validation_errors,
    )

    if validation_errors:
        raise KnowledgeAnalysisValidationError(tuple(validation_errors))

    identities = [(item.kind, normalize_knowledge_title(item.title)[1]) for item in all_candidates]
    if len(identities) != len(set(identities)):
        raise _invalid_response("Knowledge Analysis returned duplicate candidate identities.")
    return DesktopKnowledgeAnalysis(
        description,
        tuple(candidate for candidate in all_candidates if candidate.kind == "concept"),
        tuple(candidate for candidate in all_candidates if candidate.kind == "entity"),
        expected_scope,
        tuple(candidate for candidate in all_candidates if candidate.kind == "procedure"),
        document_summary,
    )


def knowledge_analysis_from_checkpoint(payload: object) -> DesktopKnowledgeAnalysis | None:
    """Restore only the current structured checkpoint contract."""
    if not isinstance(payload, dict):
        return None
    normalized = payload.get("normalized_result")
    if not isinstance(normalized, dict):
        return None
    analysis = parse_knowledge_analysis(
        json.dumps(normalized, ensure_ascii=False), aggregate="batch_count" in payload
    )
    return analysis


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
    *,
    knowledge_language: str | None = None,
) -> str:
    """Build the non-secret model input with stable Evidence IDs and source locators."""
    payload = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": KNOWLEDGE_ANALYSIS_SCOPE,
        "document_name": Path(document_name).name,
        "knowledge_language": knowledge_language,
        "evidence": [
            {
                "evidence_id": evidence_id,
                "kind": block.kind,
                "section": " / ".join(block.heading_path),
                "locator": block.locator
                or {"line_start": block.line_start, "line_end": block.line_end},
                "text": block.text,
            }
            for evidence_id, block in evidence
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _candidates(
    value: object,
    *,
    maximum_candidates: int,
    maximum_claims: int,
    maximum_sources: int,
    known_evidence_ids: frozenset[str] | None,
) -> tuple[KnowledgeAnalysisCandidate, ...]:
    if not isinstance(value, list):
        raise _invalid_response("Knowledge Analysis candidates are invalid.")
    if _exceeds_limit(value, maximum_candidates):
        raise _invalid_response(
            f"Knowledge Analysis candidates must contain at most {maximum_candidates} items."
        )
    candidates: list[KnowledgeAnalysisCandidate] = []
    for candidate_index, item in enumerate(value):
        path = f"candidates[{candidate_index}]"
        required = {
            "kind",
            "title",
            "aliases",
            "identity_labels",
            "admission",
            "claims",
        }
        if not isinstance(item, dict) or set(item) != required:
            raise _invalid_response(
                f"Knowledge Analysis {path} fields are invalid; "
                "put source_evidence_ids only inside claims."
            )
        kind_value = item.get("kind")
        if not isinstance(kind_value, str) or kind_value not in _CANDIDATE_KINDS:
            raise _invalid_response(f"Knowledge Analysis {path}.kind is invalid.")
        normalized_kind: KnowledgeAnalysisKind = kind_value  # type: ignore[assignment]
        admission_value = item.get("admission")
        if not isinstance(admission_value, str) or admission_value not in _CANDIDATE_ADMISSIONS:
            raise _invalid_response(f"Knowledge Analysis {path}.admission is invalid.")
        admission: KnowledgeCandidateAdmission = admission_value  # type: ignore[assignment]
        title = normalize_dynamic_semantic_text(
            item.get("title"),
            field=f"{path}.title",
            maximum_characters=_MAX_TITLE_CHARACTERS,
        )
        aliases = _string_list(item.get("aliases"), f"{path}.aliases")
        identity_labels = _string_list(item.get("identity_labels"), f"{path}.identity_labels")
        claims_value = item.get("claims")
        if not isinstance(claims_value, list) or not claims_value:
            raise _invalid_response(f"Knowledge Analysis {path}.claims is invalid.")
        if _exceeds_limit(claims_value, maximum_claims):
            raise _invalid_response(
                f"Knowledge Analysis {path}.claims must contain at most {maximum_claims} items."
            )
        unique_claims: list[KnowledgeAnalysisClaim] = []
        claim_indexes: dict[str, int] = {}
        for claim_index, claim_value in enumerate(claims_value):
            claim_path = f"{path}.claims[{claim_index}]"
            claim = _claim(
                claim_value,
                maximum_sources=maximum_sources,
                path=claim_path,
                known_evidence_ids=known_evidence_ids,
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
                raise _claim_source_limit_error(maximum_sources, claim_path, len(source_ids))
            unique_claims[existing_index] = KnowledgeAnalysisClaim(
                existing.text,
                source_ids,
                _merge_applicability(existing.applicability, claim.applicability),
            )
        claims = tuple(unique_claims)
        candidates.append(
            KnowledgeAnalysisCandidate(
                normalized_kind,
                title,
                aliases,
                identity_labels,
                claims,
                admission,
            )
        )
    return tuple(candidates)


def _claim(
    value: object,
    *,
    maximum_sources: int,
    path: str,
    known_evidence_ids: frozenset[str] | None,
) -> KnowledgeAnalysisClaim:
    required = {"text", "source_evidence_ids", "applicability"}
    if not isinstance(value, dict) or set(value) != required:
        raise _invalid_response(f"Knowledge Analysis {path} is invalid.")
    text = _string(value.get("text"), f"{path}.text", maximum=_MAX_CLAIM_CHARACTERS)
    source_ids = _evidence_ids(
        value.get("source_evidence_ids"),
        path=path,
        maximum_sources=maximum_sources,
        known_evidence_ids=known_evidence_ids,
    )
    if not source_ids:
        raise _invalid_response(f"Knowledge Analysis {path} must cite Evidence.")
    applicability = _applicability(
        value.get("applicability"),
        f"{path}.applicability",
        claim_evidence_ids=frozenset(source_ids),
        maximum_sources=maximum_sources,
        known_evidence_ids=known_evidence_ids,
    )
    return KnowledgeAnalysisClaim(text, source_ids, applicability)


def _summary_units(
    value: object,
    *,
    maximum_sources: int,
    maximum_units: int,
    known_evidence_ids: frozenset[str] | None,
) -> tuple[KnowledgeAnalysisSummaryUnit, ...]:
    if not isinstance(value, list):
        raise _invalid_response("Knowledge Analysis document summary is invalid.")
    if len(value) > maximum_units:
        raise _invalid_response(
            f"Knowledge Analysis document summary must contain at most {maximum_units} units."
        )
    units: list[KnowledgeAnalysisSummaryUnit] = []
    for unit_index, item in enumerate(value):
        path = f"document_summary[{unit_index}]"
        if not isinstance(item, dict) or set(item) != {
            "label",
            "text",
            "source_evidence_ids",
        }:
            raise _invalid_response(f"Knowledge Analysis {path} is invalid.")
        label = normalize_dynamic_semantic_text(
            item.get("label"),
            field=f"{path}.label",
            maximum_characters=80,
        )
        text = _string(item.get("text"), f"{path}.text", maximum=_MAX_CLAIM_CHARACTERS)
        source_ids = _evidence_ids(
            item.get("source_evidence_ids"),
            path=path,
            maximum_sources=maximum_sources,
            known_evidence_ids=known_evidence_ids,
        )
        if not source_ids:
            raise _invalid_response(f"Knowledge Analysis {path} must cite Evidence.")
        units.append(
            KnowledgeAnalysisSummaryUnit(
                label,
                text,
                source_ids,
            )
        )
    return tuple(units)


def _applicability(
    value: object,
    path: str,
    *,
    claim_evidence_ids: frozenset[str],
    maximum_sources: int,
    known_evidence_ids: frozenset[str] | None,
) -> tuple[KnowledgeClaimApplicability, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise _invalid_response(f"Knowledge Analysis {path} is invalid.")
    entries: list[KnowledgeClaimApplicability] = []
    for index, item in enumerate(value):
        entry_path = f"{path}[{index}]"
        if not isinstance(item, dict) or set(item) != {
            "dimension",
            "value",
            "source_evidence_ids",
        }:
            raise _invalid_response(f"Knowledge Analysis {entry_path} is invalid.")
        dimension = normalize_dynamic_semantic_text(
            item.get("dimension"),
            field=f"{entry_path}.dimension",
            maximum_characters=80,
        )
        scope_value = normalize_dynamic_semantic_text(
            item.get("value"),
            field=f"{entry_path}.value",
            maximum_characters=_MAX_ALIAS_OR_TAG_CHARACTERS,
        )
        evidence_ids = _evidence_ids(
            item.get("source_evidence_ids"),
            path=entry_path,
            maximum_sources=maximum_sources,
            known_evidence_ids=known_evidence_ids,
        )
        if not evidence_ids or any(
            evidence_id not in claim_evidence_ids for evidence_id in evidence_ids
        ):
            raise _invalid_response(
                f"Knowledge Analysis {entry_path} Evidence must be a subset of its claim."
            )
        entry = KnowledgeClaimApplicability(dimension, scope_value, evidence_ids)
        if entry not in entries:
            entries.append(entry)
    return tuple(entries)


def _claim_identity(claim: KnowledgeAnalysisClaim) -> str:
    scope = json.dumps(
        sorted(
            (entry.dimension.casefold(), entry.value.casefold()) for entry in claim.applicability
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{scope}\x1f{' '.join(claim.text.split()).casefold()}"


def _claim_source_limit_error(
    maximum_sources: int, path: str, actual_sources: int
) -> DesktopImportError:
    return _invalid_response(
        f"Knowledge Analysis {path}.source_evidence_ids has {actual_sources} items; "
        f"it must reference at most {maximum_sources} supplied Evidence IDs."
    )


def _evidence_ids(
    value: object,
    *,
    path: str,
    maximum_sources: int,
    known_evidence_ids: frozenset[str] | None,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _invalid_response(f"Knowledge Analysis {path}.source_evidence_ids is invalid.")
    if _exceeds_limit(value, maximum_sources):
        raise _claim_source_limit_error(maximum_sources, path, len(value))
    source_ids: list[str] = []
    for source in value:
        evidence_id = _string(source, "evidence ID", maximum=_MAX_EVIDENCE_ID_CHARACTERS)
        if known_evidence_ids is not None and evidence_id not in known_evidence_ids:
            raise _invalid_response(
                f"Knowledge Analysis {path} references unknown Evidence ID {evidence_id}."
            )
        if evidence_id not in source_ids:
            source_ids.append(evidence_id)
    return tuple(source_ids)


def _merge_applicability(
    left: tuple[KnowledgeClaimApplicability, ...],
    right: tuple[KnowledgeClaimApplicability, ...],
) -> tuple[KnowledgeClaimApplicability, ...]:
    by_scope: dict[tuple[str, str], KnowledgeClaimApplicability] = {}
    for entry in (*left, *right):
        key = (entry.dimension.casefold(), entry.value.casefold())
        existing = by_scope.get(key)
        if existing is None:
            by_scope[key] = entry
            continue
        by_scope[key] = KnowledgeClaimApplicability(
            existing.dimension,
            existing.value,
            tuple(dict.fromkeys((*existing.source_evidence_ids, *entry.source_evidence_ids))),
        )
    return tuple(by_scope[key] for key in sorted(by_scope))


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _invalid_response(f"Knowledge Analysis {field} are invalid.")
    if len(value) > _MAX_ALIAS_OR_TAG_COUNT:
        raise _invalid_response(
            f"Knowledge Analysis {field} must contain at most {_MAX_ALIAS_OR_TAG_COUNT} items."
        )
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
