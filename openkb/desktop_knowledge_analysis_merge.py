"""Deterministic Knowledge Analysis aggregation without model regeneration."""

from __future__ import annotations

import json

from openkb.desktop_document_summary import bounded_document_summary_units
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisCandidate,
    KnowledgeAnalysisClaim,
    KnowledgeAnalysisSummaryUnit,
)
from openkb.desktop_knowledge_titles import normalize_knowledge_title

_MAX_MERGED_DESCRIPTION_CHARACTERS = 4_000
_SENTENCE_ENDINGS = frozenset("。！？.!?")


def deterministic_merge_knowledge(
    analyses: tuple[DesktopKnowledgeAnalysis, ...],
) -> DesktopKnowledgeAnalysis:
    """Normalize and deduplicate exact knowledge without asking a model to reproduce it."""
    accumulators: dict[
        tuple[str, str],
        dict[str, object],
    ] = {}
    for analysis in analyses:
        for candidate in analysis.candidates:
            normalized_title = normalize_knowledge_title(candidate.title)[1]
            candidate_key = (candidate.kind, normalized_title)
            current = accumulators.setdefault(
                candidate_key,
                {
                    "candidate": candidate,
                    "aliases": [],
                    "tags": [],
                    "claims": {},
                },
            )
            _extend_unique(current["aliases"], candidate.aliases)
            _extend_unique(current["tags"], candidate.tags)
            claims = current["claims"]
            assert isinstance(claims, dict)
            for claim in candidate.claims:
                claim_key = (
                    claim.role,
                    tuple(claim.applicability.as_dict().values()),
                    _normalized_text(claim.text),
                )
                existing = claims.get(claim_key)
                if existing is None:
                    claims[claim_key] = claim
                    continue
                assert isinstance(existing, KnowledgeAnalysisClaim)
                claims[claim_key] = KnowledgeAnalysisClaim(
                    existing.text,
                    tuple(
                        dict.fromkeys((*existing.source_evidence_ids, *claim.source_evidence_ids))
                    ),
                    existing.role,
                    existing.applicability,
                )
    concepts: list[KnowledgeAnalysisCandidate] = []
    entities: list[KnowledgeAnalysisCandidate] = []
    procedures: list[KnowledgeAnalysisCandidate] = []
    for (kind, _title), current in accumulators.items():
        original = current["candidate"]
        assert isinstance(original, KnowledgeAnalysisCandidate)
        subtype = _normalized_text(original.subtype) if original.subtype else None
        claims = current["claims"]
        assert isinstance(claims, dict)
        merged = KnowledgeAnalysisCandidate(
            kind=kind,  # type: ignore[arg-type]
            title=original.title.strip(),
            aliases=tuple(current["aliases"]),  # type: ignore[arg-type]
            tags=tuple(current["tags"]),  # type: ignore[arg-type]
            claims=tuple(
                claim for claim in claims.values() if isinstance(claim, KnowledgeAnalysisClaim)
            ),
            subtype=subtype,
        )
        {"concept": concepts, "entity": entities, "procedure": procedures}[kind].append(merged)
    return DesktopKnowledgeAnalysis(
        deterministic_description(analyses),
        tuple(concepts),
        tuple(entities),
        procedures=tuple(procedures),
        document_summary=_merge_summary_units(analyses),
        corpus_ready=all(analysis.corpus_ready for analysis in analyses),
    )


def merge_split_batch_analyses(
    analyses: tuple[DesktopKnowledgeAnalysis, ...],
) -> DesktopKnowledgeAnalysis:
    """Aggregate recovered batch fragments while retaining their batch scope."""
    merged = deterministic_merge_knowledge(analyses)
    return DesktopKnowledgeAnalysis(
        merged.document_description,
        merged.concepts,
        merged.entities,
        KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
        merged.procedures,
        merged.document_summary,
        merged.corpus_ready,
    )


def deterministic_description(analyses: tuple[DesktopKnowledgeAnalysis, ...]) -> str:
    """Join unique validated descriptions under the persisted field bound."""
    values: list[str] = []
    _extend_unique(values, (analysis.document_description for analysis in analyses))
    return _bounded_description(" ".join(values))


def _merge_summary_units(
    analyses: tuple[DesktopKnowledgeAnalysis, ...],
) -> tuple[KnowledgeAnalysisSummaryUnit, ...]:
    merged: dict[tuple[str, str], KnowledgeAnalysisSummaryUnit] = {}
    for analysis in analyses:
        for unit in analysis.document_summary:
            key = unit.role, _normalized_text(unit.text)
            existing = merged.get(key)
            if existing is None:
                merged[key] = unit
                continue
            merged[key] = KnowledgeAnalysisSummaryUnit(
                existing.role,
                existing.text,
                tuple(dict.fromkeys((*existing.source_evidence_ids, *unit.source_evidence_ids))),
            )
    return bounded_document_summary_units(tuple(merged.values()))


def parse_merged_description(content: str) -> str:
    """Accept only the description projection used by model-backed merge nodes."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("Knowledge Analysis merge returned invalid JSON.") from error
    if not isinstance(payload, dict):
        raise ValueError("Knowledge Analysis merge must return one object.")
    if set(payload) == {"document_description"}:
        description = payload.get("document_description")
    elif (
        set(payload)
        == {
            "schema_version",
            "analysis_scope",
            "document_description",
            "concepts",
            "entities",
        }
        and payload.get("concepts") == []
        and payload.get("entities") == []
    ):
        # Pre-plan fixtures remain readable, but model-produced knowledge fields stay forbidden.
        description = payload.get("document_description")
    else:
        raise ValueError("Knowledge Analysis merge may return only document_description.")
    if not isinstance(description, str):
        raise ValueError("Knowledge Analysis merge description must be a string.")
    return _bounded_description(description)


def _bounded_description(value: str) -> str:
    normalized = value.strip()
    if len(normalized) <= _MAX_MERGED_DESCRIPTION_CHARACTERS:
        return normalized
    prefix = normalized[:_MAX_MERGED_DESCRIPTION_CHARACTERS]
    sentence_end = max(
        (index for index, character in enumerate(prefix) if character in _SENTENCE_ENDINGS),
        default=-1,
    )
    if sentence_end >= _MAX_MERGED_DESCRIPTION_CHARACTERS // 2:
        return prefix[: sentence_end + 1].rstrip()
    return prefix[:-1].rstrip() + "…"


def _extend_unique(target: object, values: object) -> None:
    assert isinstance(target, list)
    assert hasattr(values, "__iter__")
    seen = {_normalized_text(str(value)) for value in target}
    for value in values:  # type: ignore[union-attr]
        normalized = _normalized_text(str(value))
        if normalized and normalized not in seen:
            target.append(str(value).strip())
            seen.add(normalized)


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()
