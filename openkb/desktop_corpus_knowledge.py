"""Persist document candidates and atomically synthesize qualified corpus knowledge."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_knowledge_analysis import (
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisCandidate,
)
from openkb.desktop_knowledge_candidate_admission import assess_knowledge_candidate
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    KnowledgeGenerationSource,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_corpus_generation_in,
)
from openkb.desktop_knowledge_metadata import decode_knowledge_labels, encode_knowledge_labels
from openkb.desktop_knowledge_rendering import (
    UNSPECIFIED_APPLICABILITY,
    RenderedKnowledgeClaim,
    render_generated_knowledge,
)
from openkb.desktop_knowledge_sources import stable_source_id
from openkb.desktop_knowledge_titles import normalize_knowledge_title

CORPUS_SYNTHESIS_SCHEMA_VERSION = "openkb.corpus-knowledge.v1"
_IDENTITY_NAMESPACE = uuid.UUID("fd4bc9f7-4c24-5e43-9a93-a4e235318586")
_APPLICABILITY_DIMENSIONS = (
    "product_version",
    "platform",
    "deployment_scenario",
    "time_boundary",
)


@dataclass(frozen=True)
class _Claim:
    role: str
    text: str
    applicability: tuple[tuple[str, str], ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Candidate:
    candidate_id: str
    document_id: str
    kind: str
    title: str
    normalized_title: str
    entity_subtype: str | None
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    provenance_json: str
    claims: tuple[_Claim, ...]


def replace_document_corpus_analysis_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    analysis: DesktopKnowledgeAnalysis,
    evidence_id_map: Mapping[str, str],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    analysis_provenance_json: str,
    now: str,
) -> None:
    """Replace one Document Version's summary and candidate authority in-place."""
    if not analysis.corpus_ready:
        raise ValueError("Corpus analysis requires the extended Knowledge Analysis contract.")
    _replace_document_summary_in(
        connection,
        document_id=document_id,
        analysis=analysis,
        evidence_id_map=evidence_id_map,
        evidence=evidence,
        analysis_provenance_json=analysis_provenance_json,
        now=now,
    )
    connection.execute(
        "DELETE FROM knowledge_document_candidates WHERE document_id = ?", (document_id,)
    )
    for candidate in analysis.candidates:
        _insert_document_candidate_in(
            connection,
            document_id=document_id,
            candidate=candidate,
            evidence_id_map=evidence_id_map,
            analysis_provenance_json=analysis_provenance_json,
            now=now,
        )


def synthesize_qualified_corpus_in(
    connection: sqlite3.Connection,
    *,
    now: str,
    preferred_language: str | None = None,
) -> int | None:
    """Consolidate every admitted Available-document candidate into one snapshot."""
    candidates = _load_admitted_candidates_in(connection)
    if not candidates:
        return current_generation_id_in(connection)
    clusters = _candidate_clusters(candidates)
    connection.execute("DELETE FROM knowledge_identity_candidates")
    changes: list[KnowledgeGenerationChange] = []
    included_documents = {candidate.document_id for candidate in candidates}
    carry_forward_identity_ids: set[str] = set()
    language = _corpus_language(candidates, preferred_language=preferred_language)
    for cluster in clusters:
        change = _synthesize_cluster_in(
            connection,
            cluster,
            now=now,
            language=language,
            carry_forward_identity_ids=carry_forward_identity_ids,
        )
        if change is None:
            continue
        changes.append(change)
    if not changes:
        return current_generation_id_in(connection)
    return publish_corpus_generation_in(
        connection,
        current_generation_id=current_generation_id_in(connection),
        changes=tuple(changes),
        document_ids=tuple(sorted(included_documents)),
        carry_forward_identity_ids=tuple(sorted(carry_forward_identity_ids)),
        synthesis_schema_version=CORPUS_SYNTHESIS_SCHEMA_VERSION,
        now=now,
    )


def _replace_document_summary_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    analysis: DesktopKnowledgeAnalysis,
    evidence_id_map: Mapping[str, str],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    analysis_provenance_json: str,
    now: str,
) -> None:
    connection.execute("DELETE FROM document_summaries WHERE document_id = ?", (document_id,))
    section_map: list[dict[str, object]] = []
    section_indexes: dict[tuple[str, ...], int] = {}
    for evidence_id, block in evidence:
        canonical = evidence_id_map.get(evidence_id)
        if canonical is None:
            continue
        path = block.heading_path or ("Document",)
        index = section_indexes.get(path)
        if index is None:
            index = len(section_map)
            section_indexes[path] = index
            section_map.append({"heading_path": list(path), "evidence_ids": []})
        evidence_ids = section_map[index]["evidence_ids"]
        assert isinstance(evidence_ids, list)
        if canonical not in evidence_ids:
            evidence_ids.append(canonical)
    resolved_units = tuple(
        (unit, tuple(dict.fromkeys(evidence_id_map[value] for value in unit.source_evidence_ids)))
        for unit in analysis.document_summary
        if unit.source_evidence_ids
        and all(value in evidence_id_map for value in unit.source_evidence_ids)
    )
    connection.execute(
        """
        INSERT INTO document_summaries (
            document_id, provenance_state, section_map_json,
            analysis_provenance_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            "source_backed" if resolved_units else "structural",
            _json(section_map),
            analysis_provenance_json if resolved_units else None,
            now,
            now,
        ),
    )
    for ordinal, (unit, source_ids) in enumerate(resolved_units):
        connection.execute(
            """
            INSERT INTO document_summary_units (
                document_id, unit_ordinal, role, unit_text
            ) VALUES (?, ?, ?, ?)
            """,
            (document_id, ordinal, unit.role, unit.text),
        )
        connection.executemany(
            """
            INSERT INTO document_summary_unit_sources (
                document_id, unit_ordinal, evidence_id
            ) VALUES (?, ?, ?)
            """,
            ((document_id, ordinal, evidence_id) for evidence_id in source_ids),
        )


def _insert_document_candidate_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    candidate: KnowledgeAnalysisCandidate,
    evidence_id_map: Mapping[str, str],
    analysis_provenance_json: str,
    now: str,
) -> None:
    title, normalized_title = normalize_knowledge_title(candidate.title)
    resolved = tuple(
        (
            claim,
            tuple(dict.fromkeys(evidence_id_map[value] for value in claim.source_evidence_ids)),
        )
        for claim in candidate.claims
        if claim.source_evidence_ids
        and all(value in evidence_id_map for value in claim.source_evidence_ids)
    )
    admission = assess_knowledge_candidate(
        kind=candidate.kind,
        title=title,
        subtype=candidate.subtype,
        claims=tuple((claim.role, claim.text) for claim, _sources in resolved),
    )
    candidate_id = hashlib.sha256(
        f"{document_id}\x1f{candidate.kind}\x1f{normalized_title}".encode("utf-8")
    ).hexdigest()
    connection.execute(
        """
        INSERT INTO knowledge_document_candidates (
            candidate_id, document_id, kind, title, normalized_title,
            entity_subtype, aliases_json, tags_json, admission_state,
            admission_reason, analysis_provenance_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            document_id,
            candidate.kind,
            title,
            normalized_title,
            candidate.subtype,
            encode_knowledge_labels(candidate.aliases),
            encode_knowledge_labels(candidate.tags),
            "admitted" if admission.admitted else "rejected",
            admission.reason,
            analysis_provenance_json,
            now,
        ),
    )
    for ordinal, (claim, source_ids) in enumerate(resolved):
        connection.execute(
            """
            INSERT INTO knowledge_document_candidate_claims (
                candidate_id, claim_ordinal, role, claim_text, applicability_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                ordinal,
                claim.role,
                claim.text,
                _json(claim.applicability.as_dict()),
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_document_candidate_claim_sources (
                candidate_id, claim_ordinal, evidence_id
            ) VALUES (?, ?, ?)
            """,
            ((candidate_id, ordinal, evidence_id) for evidence_id in source_ids),
        )


def _load_admitted_candidates_in(connection: sqlite3.Connection) -> tuple[_Candidate, ...]:
    rows = connection.execute(
        """
        SELECT candidates.candidate_id, candidates.document_id, candidates.kind,
            candidates.title, candidates.normalized_title, candidates.entity_subtype,
            candidates.aliases_json, candidates.tags_json,
            candidates.analysis_provenance_json
        FROM knowledge_document_candidates AS candidates
        JOIN source_documents AS documents ON documents.document_id = candidates.document_id
        WHERE candidates.admission_state = 'admitted'
            AND documents.availability = 'available'
        ORDER BY candidates.kind, candidates.normalized_title, candidates.candidate_id
        """
    ).fetchall()
    return tuple(_candidate_from_row(connection, row) for row in rows)


def _candidate_from_row(connection: sqlite3.Connection, row: tuple[object, ...]) -> _Candidate:
    candidate_id = str(row[0])
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
    grouped: dict[int, list[tuple[object, ...]]] = defaultdict(list)
    for claim_row in claim_rows:
        grouped[int(claim_row[0])].append(claim_row)
    claims = tuple(
        _Claim(
            role=str(values[0][1]),
            text=str(values[0][2]),
            applicability=_applicability_pairs(str(values[0][3])),
            evidence_ids=tuple(str(value[4]) for value in values),
        )
        for _ordinal, values in sorted(grouped.items())
    )
    return _Candidate(
        candidate_id=candidate_id,
        document_id=str(row[1]),
        kind=str(row[2]),
        title=str(row[3]),
        normalized_title=str(row[4]),
        entity_subtype=str(row[5]) if row[5] is not None else None,
        aliases=decode_knowledge_labels(row[6]),
        tags=decode_knowledge_labels(row[7]),
        provenance_json=str(row[8]),
        claims=claims,
    )


def _candidate_clusters(candidates: tuple[_Candidate, ...]) -> tuple[tuple[_Candidate, ...], ...]:
    clusters: list[list[_Candidate]] = []
    for candidate in candidates:
        compatible = [
            index
            for index, cluster in enumerate(clusters)
            if all(_same_identity(candidate, existing) for existing in cluster)
        ]
        if len(compatible) == 1:
            clusters[compatible[0]].append(candidate)
        else:
            # Multiple possible homes are review work. Do not bridge otherwise separate
            # identities through one broad alias or tag.
            clusters.append([candidate])
    return tuple(tuple(cluster) for cluster in clusters)


def _same_identity(left: _Candidate, right: _Candidate) -> bool:
    if left.kind != right.kind or _identity_contradiction(left, right):
        return False
    if left.normalized_title == right.normalized_title:
        return True
    shared_names = _identity_tokens(left) & _identity_tokens(right)
    if len(shared_names) >= 2:
        return True
    shared_tags = {tag.casefold() for tag in left.tags if tag.strip()} & {
        tag.casefold() for tag in right.tags if tag.strip()
    }
    return bool(shared_names and shared_tags and _shared_applicability(left, right))


def _identity_contradiction(left: _Candidate, right: _Candidate) -> bool:
    if (
        left.entity_subtype
        and right.entity_subtype
        and left.entity_subtype.casefold() != right.entity_subtype.casefold()
    ):
        return True
    left_scope = _applicability_values(left)
    right_scope = _applicability_values(right)
    return any(
        left_scope[dimension].isdisjoint(right_scope[dimension])
        for dimension in left_scope.keys() & right_scope.keys()
    )


def _shared_applicability(left: _Candidate, right: _Candidate) -> bool:
    left_scope = _applicability_values(left)
    right_scope = _applicability_values(right)
    return any(
        not left_scope[dimension].isdisjoint(right_scope[dimension])
        for dimension in left_scope.keys() & right_scope.keys()
    )


def _applicability_values(candidate: _Candidate) -> dict[str, set[str]]:
    values: defaultdict[str, set[str]] = defaultdict(set)
    for claim in candidate.claims:
        for dimension, scope in claim.applicability:
            if scope.strip() and scope != UNSPECIFIED_APPLICABILITY:
                values[dimension.casefold()].add(scope.casefold())
    return dict(values)


def _synthesize_cluster_in(
    connection: sqlite3.Connection,
    cluster: tuple[_Candidate, ...],
    *,
    now: str,
    language: str,
    carry_forward_identity_ids: set[str] | None = None,
) -> KnowledgeGenerationChange | None:
    kind = cluster[0].kind
    identity_rows = _matching_identity_rows_in(connection, kind, cluster)
    if len(identity_rows) > 1:
        _record_identity_review_in(
            connection, kind, cluster, reason="multiple_identity_matches", now=now
        )
        if carry_forward_identity_ids is not None:
            carry_forward_identity_ids.update(str(row[0]) for row in identity_rows)
        return None
    if _claim_conflicts(cluster):
        _record_identity_review_in(connection, kind, cluster, reason="claim_conflict", now=now)
        if carry_forward_identity_ids is not None:
            carry_forward_identity_ids.update(str(row[0]) for row in identity_rows)
        return None
    if identity_rows:
        identity_id = str(identity_rows[0][0])
        title = str(identity_rows[0][1])
        normalized_title = str(identity_rows[0][2])
    else:
        title, normalized_title = _canonical_title(cluster)
        identity_id = uuid.uuid5(_IDENTITY_NAMESPACE, f"{kind}:{normalized_title}").hex
        connection.execute(
            """
            INSERT INTO knowledge_identities (
                identity_id, kind, canonical_title, normalized_title,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (identity_id, kind, title, normalized_title, now, now),
        )
    aliases = _cluster_aliases(cluster, normalized_title)
    connection.executemany(
        """
        INSERT INTO knowledge_identity_aliases (
            identity_id, alias, normalized_alias, created_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(identity_id, normalized_alias) DO UPDATE SET alias = excluded.alias
        """,
        ((identity_id, alias, normalize_knowledge_title(alias)[1], now) for alias in aliases),
    )
    connection.executemany(
        """
        INSERT INTO knowledge_identity_candidates (
            identity_id, candidate_id, match_basis, created_at
        ) VALUES (?, ?, 'exact_title_or_multi_signal', ?)
        """,
        ((identity_id, candidate.candidate_id, now) for candidate in cluster),
    )
    rendered_claims, sources = _merge_cluster_claims(cluster)
    content = render_generated_knowledge(kind, rendered_claims, language=language)
    if not content or not sources:
        return None
    subtype_counts = Counter(
        candidate.entity_subtype for candidate in cluster if candidate.entity_subtype
    )
    subtype = subtype_counts.most_common(1)[0][0] if subtype_counts else None
    tags = tuple(dict.fromkeys(tag for candidate in cluster for tag in candidate.tags))
    return KnowledgeGenerationChange(
        document_id=min(candidate.document_id for candidate in cluster),
        kind=kind,
        title=title,
        normalized_title=normalized_title,
        content_markdown=content,
        content_sha256=knowledge_content_sha256(content),
        entity_subtype=subtype,
        aliases=aliases,
        tags=tags,
        sources=sources,
        analysis_provenance_json=cluster[0].provenance_json,
        identity_id=identity_id,
    )


def _matching_identity_rows_in(
    connection: sqlite3.Connection,
    kind: str,
    cluster: tuple[_Candidate, ...],
) -> list[tuple[object, ...]]:
    tokens = frozenset(token for candidate in cluster for token in _identity_tokens(candidate))
    titles = frozenset(candidate.normalized_title for candidate in cluster)
    token_placeholders = ", ".join("?" for _ in tokens)
    title_placeholders = ", ".join("?" for _ in titles)
    return connection.execute(
        """
        WITH identity_names AS (
            SELECT identity_id, normalized_title AS token FROM knowledge_identities
            UNION
            SELECT identity_id, normalized_alias AS token FROM knowledge_identity_aliases
        ), matched AS (
            SELECT identity_id, COUNT(DISTINCT token) AS overlap_count
            FROM identity_names
            WHERE token IN ({token_placeholders})
            GROUP BY identity_id
        )
        SELECT identities.identity_id, identities.canonical_title,
            identities.normalized_title
        FROM knowledge_identities AS identities
        JOIN matched ON matched.identity_id = identities.identity_id
        WHERE identities.kind = ? AND (
            identities.normalized_title IN ({title_placeholders})
            OR matched.overlap_count >= 2
        )
        ORDER BY identities.identity_id
        """.format(
            token_placeholders=token_placeholders,
            title_placeholders=title_placeholders,
        ),
        (*tokens, kind, *titles),
    ).fetchall()


def _merge_cluster_claims(
    cluster: tuple[_Candidate, ...],
) -> tuple[tuple[RenderedKnowledgeClaim, ...], tuple[KnowledgeGenerationSource, ...]]:
    merged: dict[tuple[str, tuple[tuple[str, str], ...], str], tuple[_Claim, set[str]]] = {}
    for candidate in cluster:
        for claim in candidate.claims:
            key = claim.role, claim.applicability, _normalized_text(claim.text)
            current = merged.get(key)
            if current is None:
                merged[key] = (claim, set(claim.evidence_ids))
            else:
                current[1].update(claim.evidence_ids)
    rendered: list[RenderedKnowledgeClaim] = []
    claims_by_evidence: dict[str, list[str]] = defaultdict(list)
    for claim, evidence_ids in merged.values():
        ordered_ids = tuple(sorted(evidence_ids))
        rendered.append(
            RenderedKnowledgeClaim(
                text=claim.text,
                role=claim.role,
                source_markers=tuple(f"[^{stable_source_id(value)}]" for value in ordered_ids),
                applicability=claim.applicability,
            )
        )
        for evidence_id in ordered_ids:
            if claim.text not in claims_by_evidence[evidence_id]:
                claims_by_evidence[evidence_id].append(claim.text)
    sources = tuple(
        KnowledgeGenerationSource(
            source_id=stable_source_id(evidence_id),
            evidence_id=evidence_id,
            claim_text="\n".join(claims),
        )
        for evidence_id, claims in sorted(claims_by_evidence.items())
    )
    return tuple(rendered), sources


def _claim_conflicts(cluster: tuple[_Candidate, ...]) -> bool:
    claims = tuple(claim for candidate in cluster for claim in candidate.claims)
    for index, left in enumerate(claims):
        for right in claims[index + 1 :]:
            if (
                left.role == right.role
                and _normalized_text(left.text) != _normalized_text(right.text)
                and _claim_scopes_overlap(left, right)
                and _opposed_claims(left.text, right.text)
            ):
                return True
    return False


def _corpus_language(
    candidates: tuple[_Candidate, ...],
    *,
    preferred_language: str | None,
) -> str:
    if preferred_language in {"zh", "en"}:
        return preferred_language
    texts = tuple(
        claim.text for candidate in candidates for claim in candidate.claims if claim.text.strip()
    )
    chinese = sum(bool(re.search(r"[\u3400-\u9fff]", text)) for text in texts)
    return "zh" if chinese * 2 > max(1, len(texts)) else "en"


def _claim_scopes_overlap(left: _Claim, right: _Claim) -> bool:
    left_scope = dict(left.applicability)
    right_scope = dict(right.applicability)
    for dimension in left_scope.keys() & right_scope.keys():
        left_value = left_scope[dimension]
        right_value = right_scope[dimension]
        if (
            left_value != UNSPECIFIED_APPLICABILITY
            and right_value != UNSPECIFIED_APPLICABILITY
            and left_value.casefold() != right_value.casefold()
        ):
            return False
    return True


def _opposed_claims(left: str, right: str) -> bool:
    left_key, left_negative = _claim_polarity(left)
    right_key, right_negative = _claim_polarity(right)
    return bool(left_key and left_key == right_key and left_negative != right_negative)


def _claim_polarity(value: str) -> tuple[str, bool]:
    normalized = value.casefold()
    negative = False
    replacements = (
        ("不需要", "需要"),
        ("无需", "需要"),
        ("不要", ""),
        ("禁止", ""),
        ("不得", ""),
        ("不可", "可"),
        ("不能", "能"),
        ("must not", ""),
        ("do not", ""),
        ("does not", ""),
        ("should not", ""),
        ("cannot", "can"),
        ("disable", "enable"),
    )
    for marker, replacement in replacements:
        if marker in normalized:
            negative = True
            normalized = normalized.replace(marker, replacement)
    key = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized)
    return key, negative


def _identity_tokens(candidate: _Candidate) -> frozenset[str]:
    values = (candidate.title, *candidate.aliases)
    tokens = frozenset(
        normalized
        for value in values
        if (normalized := normalize_knowledge_title(value)[1])
        and (len(normalized) >= 3 or any("\u3400" <= char <= "\u9fff" for char in normalized))
    )
    return tokens or frozenset((candidate.normalized_title,))


def _canonical_title(cluster: tuple[_Candidate, ...]) -> tuple[str, str]:
    counts = Counter(candidate.title for candidate in cluster)
    title = min(counts, key=lambda value: (-counts[value], len(value), value.casefold()))
    return normalize_knowledge_title(title)


def _cluster_aliases(
    cluster: tuple[_Candidate, ...], canonical_normalized_title: str
) -> tuple[str, ...]:
    aliases: dict[str, str] = {}
    for candidate in cluster:
        for value in (candidate.title, *candidate.aliases):
            alias, normalized = normalize_knowledge_title(value)
            if normalized and normalized != canonical_normalized_title:
                aliases.setdefault(normalized, alias)
    return tuple(aliases.values())


def _record_identity_review_in(
    connection: sqlite3.Connection,
    kind: str,
    cluster: tuple[_Candidate, ...],
    *,
    reason: str,
    now: str,
) -> None:
    candidate_ids = tuple(sorted(candidate.candidate_id for candidate in cluster))
    review_id = hashlib.sha256(
        f"{kind}\x1f{reason}\x1f{'|'.join(candidate_ids)}".encode("utf-8")
    ).hexdigest()
    connection.execute(
        """
        INSERT INTO knowledge_identity_review_items (
            review_id, kind, reason, candidate_ids_json, status, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, NULL)
        ON CONFLICT(review_id) DO NOTHING
        """,
        (review_id, kind, reason, _json(candidate_ids), now),
    )


def _applicability_pairs(value: str) -> tuple[tuple[str, str], ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    return tuple(
        (
            dimension,
            str(payload.get(dimension) or UNSPECIFIED_APPLICABILITY),
        )
        for dimension in _APPLICABILITY_DIMENSIONS
    )


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
