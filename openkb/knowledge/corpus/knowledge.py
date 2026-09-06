"""Persist document candidates and atomically synthesize qualified corpus knowledge."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping

from openkb.importing.artifacts import DocumentIRBlock
from openkb.knowledge.analysis.service import DesktopKnowledgeAnalysis
from openkb.knowledge.corpus.candidate_persistence import insert_document_candidate_in
from openkb.knowledge.corpus.candidates import (
    CorpusCandidate as _Candidate,
)
from openkb.knowledge.corpus.candidates import (
    CorpusClaim as _Claim,
)
from openkb.knowledge.corpus.candidates import (
    applicability_pairs,
)
from openkb.knowledge.corpus.candidates import (
    load_admitted_candidates_in as _load_admitted_candidates_in,
)
from openkb.knowledge.corpus.identity_candidate_store import bind_identity_candidates_in
from openkb.knowledge.corpus.review_store import (
    candidate_kept_separate_in,
    has_nonliteral_cross_document_claims,
    record_review_in,
    review_decision_in,
)
from openkb.knowledge.corpus.synthesis_generation import (
    CorpusCandidateInput,
    CorpusGenerationDependencyError,
    capture_corpus_candidate_inputs_in,
)
from openkb.knowledge.pages.generations import (
    KnowledgeGenerationChange,
    KnowledgeGenerationSource,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_corpus_generation_in,
    publish_incremental_corpus_generation_in,
)
from openkb.knowledge.pages.rendering import RenderedKnowledgeClaim, render_generated_knowledge
from openkb.knowledge.pages.sources import stable_source_id
from openkb.knowledge.pages.titles import (
    normalize_knowledge_title,
)

CORPUS_SYNTHESIS_SCHEMA_VERSION = "openkb.corpus-knowledge.v1"
_IDENTITY_NAMESPACE = uuid.UUID("fd4bc9f7-4c24-5e43-9a93-a4e235318586")


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
        insert_document_candidate_in(
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
    affected_document_ids: tuple[str, ...] = (),
    candidate_inputs: tuple[CorpusCandidateInput, ...] | None = None,
    force_generation: bool = False,
    page_outcomes=None,
    defer_completion: bool = False,
) -> int | None:
    """Consolidate the full corpus or only identities affected by new documents."""
    if candidate_inputs is None:
        try:
            candidate_inputs = capture_corpus_candidate_inputs_in(connection)
        except CorpusGenerationDependencyError:
            return current_generation_id_in(connection)
    all_candidates = _load_admitted_candidates_in(connection, candidate_inputs)
    if not all_candidates:
        return current_generation_id_in(connection)
    blocked_candidate_ids = _record_uncertain_identity_reviews_in(
        connection, all_candidates, now=now
    )
    candidates = all_candidates
    if affected_document_ids:
        affected = frozenset(affected_document_ids)
        affected_keys = {
            (candidate.kind, candidate.normalized_title)
            for candidate in all_candidates
            if candidate.document_id in affected
        }
        candidates = tuple(
            candidate
            for candidate in all_candidates
            if (candidate.kind, candidate.normalized_title) in affected_keys
        )
        if not candidates:
            return current_generation_id_in(connection)
    clusters = _candidate_clusters(candidates, connection)
    if not affected_document_ids:
        connection.execute("DELETE FROM knowledge_identity_candidates")
    changes: list[KnowledgeGenerationChange] = []
    included_documents = {item.document_id for item in candidate_inputs}
    carry_forward_identity_ids: set[str] = set()
    language = _corpus_language(candidates, preferred_language=preferred_language)
    for cluster in clusters:
        if any(candidate.candidate_id in blocked_candidate_ids for candidate in cluster):
            carry_forward_identity_ids.update(
                str(row[0])
                for row in _matching_identity_rows_in(connection, cluster[0].kind, cluster)
            )
            continue
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
        current_generation_id = current_generation_id_in(connection)
        if not force_generation or current_generation_id is None:
            return current_generation_id
        return publish_incremental_corpus_generation_in(
            connection,
            current_generation_id=current_generation_id,
            changes=(),
            document_ids=tuple(sorted(included_documents)),
            synthesis_schema_version=CORPUS_SYNTHESIS_SCHEMA_VERSION,
            now=now,
            candidate_inputs=candidate_inputs,
            language=language,
            page_outcomes=page_outcomes,
            defer_completion=defer_completion,
        )
    if affected_document_ids:
        return publish_incremental_corpus_generation_in(
            connection,
            current_generation_id=current_generation_id_in(connection),
            changes=tuple(changes),
            document_ids=tuple(sorted(included_documents)),
            synthesis_schema_version=CORPUS_SYNTHESIS_SCHEMA_VERSION,
            now=now,
            candidate_inputs=candidate_inputs,
            language=language,
            page_outcomes=page_outcomes,
            defer_completion=defer_completion,
        )
    return publish_corpus_generation_in(
        connection,
        current_generation_id=current_generation_id_in(connection),
        changes=tuple(changes),
        document_ids=tuple(sorted(included_documents)),
        carry_forward_identity_ids=tuple(sorted(carry_forward_identity_ids)),
        synthesis_schema_version=CORPUS_SYNTHESIS_SCHEMA_VERSION,
        now=now,
        candidate_inputs=candidate_inputs,
        language=language,
        page_outcomes=page_outcomes,
        defer_completion=defer_completion,
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
                document_id, unit_ordinal, label, unit_text
            ) VALUES (?, ?, ?, ?)
            """,
            (document_id, ordinal, unit.label, unit.text),
        )
        connection.executemany(
            """
            INSERT INTO document_summary_unit_sources (
                document_id, unit_ordinal, evidence_id
            ) VALUES (?, ?, ?)
            """,
            ((document_id, ordinal, evidence_id) for evidence_id in source_ids),
        )


def _candidate_clusters(
    candidates: tuple[_Candidate, ...], connection=None
) -> tuple[tuple[_Candidate, ...], ...]:
    clusters: list[list[_Candidate]] = []
    for candidate in candidates:
        compatible = [
            index
            for index, cluster in enumerate(clusters)
            if all(_same_identity(candidate, existing, connection) for existing in cluster)
        ]
        if len(compatible) == 1:
            clusters[compatible[0]].append(candidate)
        else:
            # Multiple possible homes are review work. Do not bridge otherwise separate
            # identities through one broad alias or tag.
            clusters.append([candidate])
    return tuple(tuple(cluster) for cluster in clusters)


def _same_identity(left: _Candidate, right: _Candidate, connection=None) -> bool:
    if left.kind != right.kind:
        return False
    if connection is not None:
        decision = review_decision_in(
            connection, (left, right), "semantic_identity_confirmation_required"
        )
        if decision in {"same_identity", "keep_separate"}:
            return decision == "same_identity"
    if left.normalized_title == right.normalized_title:
        return True
    left_aliases = {normalize_knowledge_title(value)[1] for value in left.aliases}
    right_aliases = {normalize_knowledge_title(value)[1] for value in right.aliases}
    return left.normalized_title in right_aliases and right.normalized_title in left_aliases


def _record_uncertain_identity_reviews_in(
    connection: sqlite3.Connection,
    candidates: tuple[_Candidate, ...],
    *,
    now: str,
) -> frozenset[str]:
    """Queue plausible model-proposed matches without changing canonical identity."""
    blocked: set[str] = set()
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if left.kind != right.kind:
                continue
            if left.normalized_title == right.normalized_title:
                continue
            left_aliases = {
                normalize_knowledge_title(value)[1] for value in left.aliases if value.strip()
            }
            right_aliases = {
                normalize_knowledge_title(value)[1] for value in right.aliases if value.strip()
            }
            any_alias_match = (
                left.normalized_title in right_aliases or right.normalized_title in left_aliases
            )
            if any_alias_match and not _same_identity(left, right, connection):
                decision = review_decision_in(
                    connection, (left, right), "semantic_identity_confirmation_required"
                )
                if decision == "keep_separate":
                    continue
                _record_identity_review_in(
                    connection,
                    left.kind,
                    (left, right),
                    reason="semantic_identity_confirmation_required",
                    now=now,
                )
                blocked.update((left.candidate_id, right.candidate_id))
    return frozenset(blocked)


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
    if _claim_conflicts(connection, cluster, now=now):
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
    bind_identity_candidates_in(
        connection,
        (
            (
                identity_id,
                candidate.candidate_id,
                "exact_title",
            )
            for candidate in cluster
        ),
        now=now,
    )
    rendered_claims, sources = _merge_cluster_claims(cluster)
    content = render_generated_knowledge(kind, rendered_claims, language=language)
    if not content or not sources:
        return None
    identity_labels = tuple(
        dict.fromkeys(label for candidate in cluster for label in candidate.identity_labels)
    )
    return KnowledgeGenerationChange(
        document_id=min(candidate.document_id for candidate in cluster),
        kind=kind,
        title=title,
        normalized_title=normalized_title,
        content_markdown=content,
        content_sha256=knowledge_content_sha256(content),
        aliases=aliases,
        identity_labels=identity_labels,
        sources=sources,
        analysis_provenance_json=cluster[0].provenance_json,
        identity_id=identity_id,
    )


def _matching_identity_rows_in(
    connection: sqlite3.Connection,
    kind: str,
    cluster: tuple[_Candidate, ...],
) -> list[tuple[object, ...]]:
    titles = {candidate.normalized_title for candidate in cluster if candidate.normalized_title}
    if any(candidate_kept_separate_in(connection, candidate) for candidate in cluster):
        placeholders = ", ".join("?" for _ in titles)
        return connection.execute(
            "SELECT identity_id, canonical_title, normalized_title FROM knowledge_identities "
            f"WHERE kind = ? AND normalized_title IN ({placeholders}) ORDER BY identity_id",
            (kind, *sorted(titles)),
        ).fetchall()
    canonical_normalized = _canonical_title(cluster)[1]
    titles.update(
        normalize_knowledge_title(alias)[1]
        for alias in _cluster_aliases(cluster, canonical_normalized)
    )
    title_placeholders = ", ".join("?" for _ in titles)
    return connection.execute(
        """
        SELECT DISTINCT identities.identity_id, identities.canonical_title,
            identities.normalized_title
        FROM knowledge_identities AS identities
        LEFT JOIN knowledge_identity_aliases AS aliases
          ON aliases.identity_id = identities.identity_id
        WHERE identities.kind = ?
          AND (
            identities.normalized_title IN ({title_placeholders})
            OR aliases.normalized_alias IN ({title_placeholders})
          )
        ORDER BY identities.identity_id
        """.format(
            title_placeholders=title_placeholders,
        ),
        (kind, *titles, *titles),
    ).fetchall()


def _merge_cluster_claims(
    cluster: tuple[_Candidate, ...],
) -> tuple[tuple[RenderedKnowledgeClaim, ...], tuple[KnowledgeGenerationSource, ...]]:
    merged: dict[tuple[tuple[tuple[str, str], ...], str], tuple[_Claim, set[str]]] = {}
    for candidate in cluster:
        for claim in candidate.claims:
            key = claim.applicability, _normalized_text(claim.text)
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


def _claim_conflicts(
    connection: sqlite3.Connection, cluster: tuple[_Candidate, ...], *, now: str
) -> bool:
    """Hold nonliteral cross-document comparisons until a bound judgment permits them."""
    if not has_nonliteral_cross_document_claims(cluster):
        return False
    record_review_in(connection, cluster, "claim_relationship_review", now)
    return review_decision_in(connection, cluster, "claim_relationship_review") != "compatible"


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
    record_review_in(connection, cluster, reason, now)


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _applicability_pairs(value: str) -> tuple[tuple[str, str], ...]:
    return applicability_pairs(value)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
