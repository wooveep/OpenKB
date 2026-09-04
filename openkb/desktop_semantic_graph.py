"""Plan and validate a canonical graph over admitted Knowledge Identities."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from openkb.desktop_candidate_registry import (
    CandidateRegistryStatus,
    candidate_registry_outcome_in,
)
from openkb.desktop_knowledge_graph_interpretation import (
    GraphDispositionCounts,
    KnowledgeGraphIssue,
)
from openkb.desktop_knowledge_metadata import decode_knowledge_labels
from openkb.desktop_model_execution_profile import estimate_model_tokens
from openkb.desktop_semantic_graph_contract import (
    MAX_SEMANTIC_IDENTIFIER_CHARS,
    MAX_SEMANTIC_RELATIONS_PER_BATCH,
    MAX_SEMANTIC_SUPPORT_CLAIMS,
    SEMANTIC_GRAPH_RELATION_KINDS,
    relation_endpoint_allowed,
)
from openkb.desktop_structured_output import normalize_structured_output

SEMANTIC_RELATION_OPERATION = "knowledge_relation_analysis"
SEMANTIC_GRAPH_SCHEMA_VERSION = "openkb.semantic-identity-graph.v1"
_INPUT_SCHEMA_VERSION = "openkb.semantic-relation-input.v1"
_MAX_CLAIMS_PER_BATCH = 64
_MAX_ELIGIBLE_ENDPOINT_MENTIONS_PER_BATCH = 64
_MAX_RESPONSE_CHARS = 1_000_000
_RELATION_FIELDS = frozenset(
    {"source_candidate_id", "target_candidate_id", "type", "supporting_claims"}
)
_SUPPORT_FIELDS = frozenset({"candidate_id", "claim_ordinal"})


class SemanticGraphCapacityError(ValueError):
    """The complete candidate registry cannot fit the selected model input."""


class SemanticGraphStoredDataError(ValueError):
    """Stored semantic graph input violates the persisted-data contract."""


@dataclass(frozen=True)
class SemanticGraphClaim:
    candidate_id: str
    claim_ordinal: int
    role: str
    text: str
    applicability_json: str
    evidence_ids: tuple[str, ...]

    @property
    def key(self) -> tuple[str, int]:
        return self.candidate_id, self.claim_ordinal


@dataclass(frozen=True)
class SemanticGraphCandidate:
    candidate_id: str
    kind: str
    title: str
    aliases: tuple[str, ...]
    entity_subtype: str | None
    claims: tuple[SemanticGraphClaim, ...]


@dataclass(frozen=True)
class SemanticGraphDocument:
    document_id: str
    document_name: str
    candidates: tuple[SemanticGraphCandidate, ...]
    candidate_generation_id: str | None = None
    candidate_generation_digest: str | None = None

    @property
    def claims(self) -> tuple[SemanticGraphClaim, ...]:
        return tuple(claim for candidate in self.candidates for claim in candidate.claims)


@dataclass(frozen=True)
class SemanticGraphInputOutcome:
    """Closed graph dependency route for one Document Version."""

    status: CandidateRegistryStatus
    document: SemanticGraphDocument | None = None


@dataclass(frozen=True)
class SemanticRelationBatch:
    document: SemanticGraphDocument
    ordinal: int
    claims: tuple[SemanticGraphClaim, ...]
    source_material: str
    estimated_input_tokens: int


@dataclass(frozen=True)
class SemanticClaimReference:
    candidate_id: str
    claim_ordinal: int


@dataclass(frozen=True)
class SemanticRelation:
    source_candidate_id: str
    target_candidate_id: str
    relation_kind: str
    supporting_claims: tuple[SemanticClaimReference, ...]
    assertion_evidence_ids: tuple[str, ...]
    applicability_json: str


@dataclass(frozen=True)
class SemanticGraphInterpretation:
    relations: tuple[SemanticRelation, ...]
    lifecycle: str
    quality: str | None
    issues: tuple[KnowledgeGraphIssue, ...]
    counts: GraphDispositionCounts
    repairable: bool = False
    failure_signature: str | None = None

    @property
    def payload(self) -> tuple[SemanticRelation, ...] | None:
        """Compatibility shape for shared content-free attempt persistence."""
        return None if self.lifecycle == "failed" else self.relations


class SemanticRelationInterpretationError(ValueError):
    """A content-free summary of an unusable semantic relation response."""

    def __init__(self, interpretation: SemanticGraphInterpretation) -> None:
        self.interpretation = interpretation
        details = "; ".join(f"{issue.code} at {issue.path}" for issue in interpretation.issues)
        super().__init__(details or "Semantic relation response cannot be interpreted safely.")


def load_semantic_graph_document_in(
    connection: sqlite3.Connection, document_id: str
) -> SemanticGraphDocument | None:
    """Compatibility projection for callers that only consume semantic input."""
    return load_semantic_graph_input_in(connection, document_id).document


def load_semantic_graph_input_in(
    connection: sqlite3.Connection, document_id: str
) -> SemanticGraphInputOutcome:
    """Load the exact current Candidate Registry Generation or a closed outcome."""
    document = connection.execute(
        "SELECT display_name FROM source_documents "
        "WHERE document_id = ? AND availability = 'available'",
        (document_id,),
    ).fetchone()
    if document is None:
        return SemanticGraphInputOutcome("dependency_unavailable")
    registry = candidate_registry_outcome_in(connection, document_id)
    if registry.status in {"dependency_unavailable", "explicit_legacy"}:
        return SemanticGraphInputOutcome(registry.status)
    assert registry.generation is not None
    generation = registry.generation
    rows = connection.execute(
        """
        SELECT candidate_id, kind, title, aliases_json, entity_subtype
        FROM knowledge_candidate_generation_candidates
        WHERE candidate_generation_id = ? AND admission_state = 'admitted'
        ORDER BY kind, normalized_title, candidate_id
        """,
        (generation.generation_id,),
    ).fetchall()
    candidates = tuple(
        _candidate_in(connection, row, candidate_generation_id=generation.generation_id)
        for row in rows
    )
    graph_document = SemanticGraphDocument(
        document_id,
        str(document[0]),
        candidates,
        generation.generation_id,
        generation.registry_digest,
    )
    return SemanticGraphInputOutcome(registry.status, graph_document)


def semantic_graph_operation_for_document_in(
    connection: sqlite3.Connection, document_id: str
) -> str:
    """Choose from explicit provenance; never infer legacy from an empty candidate table."""
    outcome = candidate_registry_outcome_in(connection, document_id)
    if outcome.status == "explicit_legacy":
        return "knowledge_graph_extraction"
    return SEMANTIC_RELATION_OPERATION


def plan_semantic_relation_batches(
    document: SemanticGraphDocument,
    *,
    input_budget_tokens: int,
) -> tuple[SemanticRelationBatch, ...]:
    """Cover every admitted claim in bounded requests without prefix truncation."""
    if input_budget_tokens <= 0:
        raise SemanticGraphCapacityError("Semantic relation input budget must be positive.")
    claims = document.claims
    if not claims:
        return ()
    batches: list[SemanticRelationBatch] = []
    current: list[SemanticGraphClaim] = []
    for claim in claims:
        proposed = (*current, claim)
        material = _source_material(document, proposed, ordinal=len(batches))
        tokens = estimate_model_tokens(material)
        endpoint_mentions = _eligible_endpoint_mention_count(document, proposed)
        if current and (
            tokens > input_budget_tokens
            or len(proposed) > _MAX_CLAIMS_PER_BATCH
            or endpoint_mentions > _MAX_ELIGIBLE_ENDPOINT_MENTIONS_PER_BATCH
        ):
            batches.append(_batch(document, len(batches), tuple(current), input_budget_tokens))
            current = [claim]
            continue
        if tokens > input_budget_tokens:
            raise SemanticGraphCapacityError(
                "The eligible semantic candidate registry and one claim exceed the model input "
                "budget; choose a larger-context Analysis model."
            )
        current.append(claim)
    if current:
        batches.append(_batch(document, len(batches), tuple(current), input_budget_tokens))
    return tuple(batches)


def semantic_relation_sub_batch(
    parent: SemanticRelationBatch,
    claims: tuple[SemanticGraphClaim, ...],
) -> SemanticRelationBatch:
    """Rebuild one strict, non-empty subset for output-limit recovery."""
    if not claims or len(claims) >= len(parent.claims):
        raise SemanticGraphCapacityError("Semantic relation recovery subset is invalid.")
    parent_keys = tuple(claim.key for claim in parent.claims)
    child_keys = tuple(claim.key for claim in claims)
    positions = [parent_keys.index(key) for key in child_keys if key in parent_keys]
    if len(positions) != len(child_keys) or positions != sorted(set(positions)):
        raise SemanticGraphCapacityError("Semantic relation recovery changed claim coverage.")
    return _batch(
        parent.document,
        parent.ordinal,
        claims,
        parent.estimated_input_tokens,
    )


class SemanticRelationBoundary:
    """Interpret relations at the only model-output-to-identity-graph seam."""

    @staticmethod
    def interpret(
        content: str,
        batch: SemanticRelationBatch,
        *,
        reject_partial: bool = True,
        allow_empty_degraded: bool = False,
    ) -> SemanticGraphInterpretation:
        if len(content) > _MAX_RESPONSE_CHARS:
            return _fatal("response_budget_exceeded", "$", "budget")
        try:
            value = json.loads(normalize_structured_output(content))
        except (json.JSONDecodeError, RecursionError, ValueError):
            return _fatal("invalid_json", "$", "shape")
        if not isinstance(value, dict):
            return _fatal("top_level_not_object", "$", "shape")
        if set(value) != {"relations"}:
            return _fatal("invalid_top_level_fields", "$", "shape")
        raw_relations = value.get("relations")
        if not isinstance(raw_relations, list):
            return _fatal("invalid_relations_array", "$.relations", "shape")
        if len(raw_relations) > MAX_SEMANTIC_RELATIONS_PER_BATCH:
            return _fatal("relation_payload_budget_exceeded", "$.relations", "budget")
        candidates = {candidate.candidate_id: candidate for candidate in batch.document.candidates}
        claims = {claim.key: claim for claim in batch.claims}
        retained: dict[tuple[str, str, str], SemanticRelation] = {}
        issues: list[KnowledgeGraphIssue] = []
        for index, raw in enumerate(raw_relations):
            try:
                relation = _relation(raw, index, candidates, claims)
            except _RelationProblem as problem:
                issues.append(problem.issue)
                continue
            key = (
                relation.source_candidate_id,
                relation.target_candidate_id,
                relation.relation_kind,
            )
            existing = retained.get(key)
            retained[key] = relation if existing is None else _merge_relation(existing, relation)
        relations = tuple(retained.values())
        if issues and (reject_partial or (not relations and not allow_empty_degraded)):
            return _failed_from_issues(tuple(issues))
        return SemanticGraphInterpretation(
            relations=relations,
            lifecycle="completed",
            quality="degraded" if issues else "full",
            issues=tuple(issues),
            counts=GraphDispositionCounts(
                retained=len(relations),
                weakened=0,
                rejected=len(issues),
            ),
        )


def merge_semantic_relation_interpretations(
    document: SemanticGraphDocument,
    interpretations: tuple[SemanticGraphInterpretation, ...],
) -> SemanticGraphInterpretation:
    """Merge complete batch outcomes into one document-scoped graph publication."""
    failed = next((item for item in interpretations if item.lifecycle == "failed"), None)
    if failed is not None:
        return failed
    retained: dict[tuple[str, str, str], SemanticRelation] = {}
    issues: list[KnowledgeGraphIssue] = []
    for interpretation in interpretations:
        issues.extend(interpretation.issues)
        for relation in interpretation.relations:
            key = (
                relation.source_candidate_id,
                relation.target_candidate_id,
                relation.relation_kind,
            )
            existing = retained.get(key)
            retained[key] = relation if existing is None else _merge_relation(existing, relation)
    relations = tuple(retained.values())
    return SemanticGraphInterpretation(
        relations=relations,
        lifecycle="completed" if document.candidates else "completed_empty",
        quality="degraded" if issues else "full",
        issues=tuple(issues),
        counts=GraphDispositionCounts(
            retained=len(document.candidates) + len(relations),
            weakened=0,
            rejected=len(issues),
        ),
    )


def replace_document_semantic_relations_in(
    connection: sqlite3.Connection,
    document: SemanticGraphDocument,
    interpretation: SemanticGraphInterpretation,
    *,
    graph_result_id: str,
) -> None:
    """Replace one document's validated relation assertions inside the caller transaction."""
    if interpretation.lifecycle not in {"completed", "completed_empty"}:
        raise ValueError("Only completed semantic graph results can be published.")
    connection.execute(
        "DELETE FROM knowledge_document_relationships WHERE document_id = ?",
        (document.document_id,),
    )
    for relation in interpretation.relations:
        connection.execute(
            """
            INSERT INTO knowledge_document_relationships (
                document_id, source_candidate_id, target_candidate_id,
                relation_kind, applicability_json, provenance,
                candidate_generation_id, graph_result_id
            ) VALUES (?, ?, ?, ?, ?, 'semantic_relation_analysis', ?, ?)
            """,
            (
                document.document_id,
                relation.source_candidate_id,
                relation.target_candidate_id,
                relation.relation_kind,
                relation.applicability_json,
                document.candidate_generation_id,
                graph_result_id,
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_document_relationship_claims (
                document_id, source_candidate_id, target_candidate_id,
                relation_kind, support_candidate_id, claim_ordinal
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    document.document_id,
                    relation.source_candidate_id,
                    relation.target_candidate_id,
                    relation.relation_kind,
                    support.candidate_id,
                    support.claim_ordinal,
                )
                for support in relation.supporting_claims
            ),
        )


def _candidate_in(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
    *,
    candidate_generation_id: str | None = None,
) -> SemanticGraphCandidate:
    candidate_id = str(row[0])
    if candidate_generation_id is None:
        claim_rows = connection.execute(
            """
            SELECT claims.claim_ordinal, claims.role, claims.claim_text,
                claims.applicability_json, sources.evidence_id
            FROM knowledge_document_candidate_claims AS claims
            JOIN knowledge_document_candidate_claim_sources AS sources
              ON sources.candidate_id = claims.candidate_id
             AND sources.claim_ordinal = claims.claim_ordinal
            WHERE claims.candidate_id = ?
            ORDER BY claims.claim_ordinal, sources.evidence_id
            """,
            (candidate_id,),
        ).fetchall()
    else:
        claim_rows = connection.execute(
            """
            SELECT claims.claim_ordinal, claims.role, claims.claim_text,
                claims.applicability_json, sources.evidence_id
            FROM knowledge_candidate_generation_claims AS claims
            JOIN knowledge_candidate_generation_claim_sources AS sources
              ON sources.candidate_generation_id = claims.candidate_generation_id
             AND sources.candidate_id = claims.candidate_id
             AND sources.claim_ordinal = claims.claim_ordinal
            WHERE claims.candidate_generation_id = ? AND claims.candidate_id = ?
            ORDER BY claims.claim_ordinal, sources.evidence_id
            """,
            (candidate_generation_id, candidate_id),
        ).fetchall()
    grouped: dict[int, list[tuple[object, ...]]] = {}
    for claim_row in claim_rows:
        grouped.setdefault(int(claim_row[0]), []).append(claim_row)
    claims = tuple(
        SemanticGraphClaim(
            candidate_id=candidate_id,
            claim_ordinal=ordinal,
            role=str(values[0][1]),
            text=str(values[0][2]),
            applicability_json=str(values[0][3]),
            evidence_ids=tuple(dict.fromkeys(str(value[4]) for value in values)),
        )
        for ordinal, values in sorted(grouped.items())
    )
    return SemanticGraphCandidate(
        candidate_id=candidate_id,
        kind=str(row[1]),
        title=str(row[2]),
        aliases=decode_knowledge_labels(row[3]),
        entity_subtype=str(row[4]) if row[4] is not None else None,
        claims=claims,
    )


def _batch(
    document: SemanticGraphDocument,
    ordinal: int,
    claims: tuple[SemanticGraphClaim, ...],
    input_budget_tokens: int,
) -> SemanticRelationBatch:
    if not claims:
        raise SemanticGraphCapacityError("Semantic relation batch must contain a claim.")
    if len(claims) > _MAX_CLAIMS_PER_BATCH:
        raise SemanticGraphCapacityError("Semantic relation batch exceeds its claim limit.")
    if (
        _eligible_endpoint_mention_count(document, claims)
        > _MAX_ELIGIBLE_ENDPOINT_MENTIONS_PER_BATCH
    ):
        raise SemanticGraphCapacityError(
            "Semantic relation batch exceeds its endpoint mention limit."
        )
    material = _source_material(document, claims, ordinal=ordinal)
    tokens = estimate_model_tokens(material)
    if tokens > input_budget_tokens:
        raise SemanticGraphCapacityError("Semantic relation batch exceeds its input budget.")
    return SemanticRelationBatch(document, ordinal, claims, material, tokens)


def _source_material(
    document: SemanticGraphDocument,
    claims: tuple[SemanticGraphClaim, ...] | list[SemanticGraphClaim],
    *,
    ordinal: int,
) -> str:
    eligible_candidates = _eligible_candidates(document, claims)
    payload = {
        "schema_version": _INPUT_SCHEMA_VERSION,
        "document_id": document.document_id,
        "document_name": document.document_name,
        "batch_ordinal": ordinal,
        "candidate_registry": [
            {
                "candidate_id": candidate.candidate_id,
                "kind": candidate.kind,
                "title": candidate.title,
                "aliases": list(candidate.aliases),
                "entity_subtype": candidate.entity_subtype or "",
            }
            for candidate in eligible_candidates
        ],
        "claims": [
            {
                "candidate_id": claim.candidate_id,
                "claim_ordinal": claim.claim_ordinal,
                "role": claim.role,
                "text": claim.text,
                "applicability": _json_value(claim.applicability_json),
                "source_evidence_ids": list(claim.evidence_ids),
            }
            for claim in claims
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _eligible_candidates(
    document: SemanticGraphDocument,
    claims: tuple[SemanticGraphClaim, ...] | list[SemanticGraphClaim],
) -> tuple[SemanticGraphCandidate, ...]:
    """Return every identity that can be an endpoint under the literal-support rule."""
    source_ids = {claim.candidate_id for claim in claims}
    claim_texts = tuple(claim.text for claim in claims)
    return tuple(
        candidate
        for candidate in document.candidates
        if candidate.candidate_id in source_ids
        or any(_candidate_named_in_text(candidate, text) for text in claim_texts)
    )


def _eligible_endpoint_mention_count(
    document: SemanticGraphDocument,
    claims: tuple[SemanticGraphClaim, ...] | list[SemanticGraphClaim],
) -> int:
    return sum(
        candidate.candidate_id != claim.candidate_id
        and _candidate_named_in_text(candidate, claim.text)
        for claim in claims
        for candidate in document.candidates
    )


def _relation(
    value: object,
    index: int,
    candidates: dict[str, SemanticGraphCandidate],
    claims: dict[tuple[str, int], SemanticGraphClaim],
) -> SemanticRelation:
    path = f"relations[{index}]"
    if not isinstance(value, dict) or set(value) != _RELATION_FIELDS:
        raise _problem("invalid_relation_fields", path, "shape")
    source_id = _identifier(value.get("source_candidate_id"), f"{path}.source_candidate_id")
    target_id = _identifier(value.get("target_candidate_id"), f"{path}.target_candidate_id")
    relation_kind = _identifier(value.get("type"), f"{path}.type")
    source = candidates.get(source_id)
    target = candidates.get(target_id)
    if source is None:
        raise _problem("unknown_source_candidate", f"{path}.source_candidate_id", "semantic")
    if target is None:
        raise _problem("unknown_target_candidate", f"{path}.target_candidate_id", "semantic")
    if source_id == target_id:
        raise _problem("self_relation", path, "semantic")
    if relation_kind not in SEMANTIC_GRAPH_RELATION_KINDS:
        raise _problem("unsupported_relationship", f"{path}.type", "semantic")
    if not relation_endpoint_allowed(relation_kind, source.kind, target.kind):
        raise _problem("incompatible_relation_endpoints", path, "semantic")
    raw_supports = value.get("supporting_claims")
    if (
        not isinstance(raw_supports, list)
        or not raw_supports
        or len(raw_supports) > MAX_SEMANTIC_SUPPORT_CLAIMS
    ):
        raise _problem("invalid_supporting_claims", f"{path}.supporting_claims", "evidence")
    supports: list[SemanticClaimReference] = []
    support_claims: list[SemanticGraphClaim] = []
    for support_index, raw_support in enumerate(raw_supports):
        support_path = f"{path}.supporting_claims[{support_index}]"
        if not isinstance(raw_support, dict) or set(raw_support) != _SUPPORT_FIELDS:
            raise _problem("invalid_supporting_claim", support_path, "shape")
        candidate_id = _identifier(raw_support.get("candidate_id"), f"{support_path}.candidate_id")
        ordinal = raw_support.get("claim_ordinal")
        if type(ordinal) is not int or ordinal < 0:
            raise _problem("invalid_claim_ordinal", f"{support_path}.claim_ordinal", "shape")
        claim = claims.get((candidate_id, ordinal))
        if claim is None:
            raise _problem("unknown_supporting_claim", support_path, "evidence")
        if candidate_id not in {source_id, target_id}:
            raise _problem("support_not_endpoint_bound", support_path, "evidence")
        if not _claim_supports_endpoints(claim, source, target):
            raise _problem("support_does_not_name_other_endpoint", support_path, "evidence")
        reference = SemanticClaimReference(candidate_id, ordinal)
        if reference not in supports:
            supports.append(reference)
            support_claims.append(claim)
    evidence_ids = tuple(
        dict.fromkeys(evidence_id for claim in support_claims for evidence_id in claim.evidence_ids)
    )
    applicability = tuple(
        dict.fromkeys(
            _canonical_json(_json_value(claim.applicability_json)) for claim in support_claims
        )
    )
    return SemanticRelation(
        source_candidate_id=source_id,
        target_candidate_id=target_id,
        relation_kind=relation_kind,
        supporting_claims=tuple(supports),
        assertion_evidence_ids=evidence_ids,
        applicability_json=_canonical_json([_json_value(value) for value in applicability]),
    )


def _claim_supports_endpoints(
    claim: SemanticGraphClaim,
    source: SemanticGraphCandidate,
    target: SemanticGraphCandidate,
) -> bool:
    other = target if claim.candidate_id == source.candidate_id else source
    return _candidate_named_in_text(other, claim.text)


def _candidate_named_in_text(candidate: SemanticGraphCandidate, text: str) -> bool:
    folded = " ".join(text.casefold().split())
    names = (candidate.title, *candidate.aliases)
    return any(
        (normalized := " ".join(name.casefold().split())) and normalized in folded for name in names
    )


def _merge_relation(left: SemanticRelation, right: SemanticRelation) -> SemanticRelation:
    applicability = tuple(
        dict.fromkeys(
            (
                *(_canonical_json(value) for value in _json_list(left.applicability_json)),
                *(_canonical_json(value) for value in _json_list(right.applicability_json)),
            )
        )
    )
    return SemanticRelation(
        source_candidate_id=left.source_candidate_id,
        target_candidate_id=left.target_candidate_id,
        relation_kind=left.relation_kind,
        supporting_claims=tuple(dict.fromkeys((*left.supporting_claims, *right.supporting_claims))),
        assertion_evidence_ids=tuple(
            dict.fromkeys((*left.assertion_evidence_ids, *right.assertion_evidence_ids))
        ),
        applicability_json=_canonical_json([_json_value(value) for value in applicability]),
    )


def _identifier(value: object, path: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_SEMANTIC_IDENTIFIER_CHARS
    ):
        raise _problem("invalid_identifier", path, "shape")
    return value


class _RelationProblem(ValueError):
    def __init__(self, issue: KnowledgeGraphIssue) -> None:
        self.issue = issue
        super().__init__(f"{issue.code} at {issue.path}")


def _problem(code: str, path: str, failure_class: str) -> _RelationProblem:
    return _RelationProblem(KnowledgeGraphIssue(code, path, "rejected", failure_class))


def _fatal(code: str, path: str, failure_class: str) -> SemanticGraphInterpretation:
    issues = (KnowledgeGraphIssue(code, path, "fatal", failure_class),)
    return _failed_from_issues(issues)


def _failed_from_issues(
    issues: tuple[KnowledgeGraphIssue, ...],
) -> SemanticGraphInterpretation:
    encoded = json.dumps(
        [(issue.code, issue.path, issue.failure_class) for issue in issues],
        separators=(",", ":"),
    ).encode()
    return SemanticGraphInterpretation(
        relations=(),
        lifecycle="failed",
        quality=None,
        issues=issues,
        counts=GraphDispositionCounts(
            retained=0,
            weakened=sum(issue.disposition == "weakened" for issue in issues),
            rejected=sum(issue.disposition == "rejected" for issue in issues),
        ),
        repairable=True,
        failure_signature=f"sgr:{hashlib.sha256(encoded).hexdigest()}",
    )


def _json_value(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise SemanticGraphStoredDataError(
            "Semantic graph applicability is invalid JSON."
        ) from error


def _json_list(value: str) -> list[object]:
    parsed = _json_value(value)
    if not isinstance(parsed, list):
        raise SemanticGraphStoredDataError("Semantic graph applicability must be a JSON array.")
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
