"""Persist document candidates and atomically synthesize qualified corpus knowledge."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping

from openkb.desktop_corpus_candidate_persistence import insert_document_candidate_in
from openkb.desktop_corpus_candidates import (
    CorpusCandidate as _Candidate,
)
from openkb.desktop_corpus_candidates import (
    CorpusClaim as _Claim,
)
from openkb.desktop_corpus_candidates import (
    applicability_pairs,
)
from openkb.desktop_corpus_candidates import (
    load_admitted_candidates_in as _load_admitted_candidates_in,
)
from openkb.desktop_corpus_synthesis_generation import (
    CorpusCandidateInput,
    CorpusGenerationDependencyError,
    capture_corpus_candidate_inputs_in,
)
from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_knowledge_analysis import DesktopKnowledgeAnalysis
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    KnowledgeGenerationSource,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_corpus_generation_in,
    publish_incremental_corpus_generation_in,
)
from openkb.desktop_knowledge_rendering import (
    UNSPECIFIED_APPLICABILITY,
    RenderedKnowledgeClaim,
    render_generated_knowledge,
)
from openkb.desktop_knowledge_sources import stable_source_id
from openkb.desktop_knowledge_titles import (
    claim_explicitly_supports_alias,
    controlled_latin_title_key,
    normalize_knowledge_title,
)

CORPUS_SYNTHESIS_SCHEMA_VERSION = "openkb.corpus-knowledge.v1"
_IDENTITY_NAMESPACE = uuid.UUID("fd4bc9f7-4c24-5e43-9a93-a4e235318586")
_CONFLICT_VALUE = re.compile(
    r"(?<![0-9a-z_])(?:v?\d+(?:\.\d+){0,3}|true|false|enabled?|disabled?|on|off)"
    r"(?![0-9a-z_])",
    re.IGNORECASE,
)
_CONFLICT_VALUE_CONTEXT = re.compile(
    r"port|default|timeout|version|address|gateway|mask|replica|端口|默认|超时|版本|"
    r"地址|网关|掩码|副本|数值|取值",
    re.IGNORECASE,
)


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
    dossier_planner=None,
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
    clusters = _candidate_clusters(candidates)
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
            dossier_planner=dossier_planner,
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
            dossier_planner=dossier_planner,
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
        dossier_planner=dossier_planner,
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
    if controlled_latin_title_key(left.title) != controlled_latin_title_key(right.title):
        return False
    return any(
        claim_explicitly_supports_alias(left.title, right.title, claim.text)
        for candidate in (left, right)
        for claim in candidate.claims
    )


def _identity_contradiction(left: _Candidate, right: _Candidate) -> bool:
    if (
        left.entity_subtype
        and right.entity_subtype
        and left.entity_subtype.casefold() != right.entity_subtype.casefold()
    ):
        return True
    return False


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
                if _identity_contradiction(left, right):
                    _record_identity_review_in(
                        connection,
                        left.kind,
                        (left, right),
                        reason="identity_contradiction",
                        now=now,
                    )
                    blocked.update((left.candidate_id, right.candidate_id))
                continue
            left_aliases = {
                normalize_knowledge_title(value)[1] for value in left.aliases if value.strip()
            }
            right_aliases = {
                normalize_knowledge_title(value)[1] for value in right.aliases if value.strip()
            }
            reciprocal_alias = (
                left.normalized_title in right_aliases and right.normalized_title in left_aliases
            )
            shared_names = _identity_tokens(left) & _identity_tokens(right)
            shared_tags = {tag.casefold() for tag in left.tags if tag.strip()} & {
                tag.casefold() for tag in right.tags if tag.strip()
            }
            if reciprocal_alias or (len(shared_names) >= 2 and shared_tags):
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
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(identity_id, candidate_id) DO UPDATE SET
            match_basis = excluded.match_basis,
            created_at = excluded.created_at
        """,
        (
            (
                identity_id,
                candidate.candidate_id,
                (
                    "inventory_target"
                    if candidate.inventory_target_identity_id is not None
                    else "exact_title"
                ),
                now,
            )
            for candidate in cluster
        ),
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
    targeted_candidates = tuple(
        candidate
        for candidate in cluster
        if candidate.inventory_target_identity_id is not None
        or candidate.inventory_target_generation_id is not None
    )
    target_identity_ids = {
        candidate.inventory_target_identity_id
        for candidate in targeted_candidates
        if candidate.inventory_target_identity_id is not None
    }
    if target_identity_ids:
        if any(
            candidate.inventory_target_identity_id is None
            or candidate.inventory_target_generation_id is None
            for candidate in targeted_candidates
        ):
            raise ValueError("Inventory target identity and generation must remain paired.")
        target_generation_ids = {
            candidate.inventory_target_generation_id for candidate in targeted_candidates
        }
        current_row = connection.execute(
            "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
        ).fetchone()
        current_generation_id = (
            int(current_row[0])
            if current_row is not None
            and current_row[0] is not None
            and type(current_row[0]) is int
            else None
        )
        if len(target_generation_ids) != 1 or current_generation_id not in target_generation_ids:
            raise ValueError("Inventory target generation is no longer current.")
        placeholders = ", ".join("?" for _identity_id in target_identity_ids)
        rows = connection.execute(
            "SELECT DISTINCT identities.identity_id, identities.canonical_title, "
            "identities.normalized_title FROM knowledge_identities AS identities "
            "JOIN knowledge_generation_items AS items "
            "ON items.identity_id = identities.identity_id "
            "WHERE identities.kind = ? AND identities.identity_id IN "
            f"({placeholders}) AND identities.status = 'active' "
            "AND items.generation_id = ? ORDER BY identities.identity_id",
            (kind, *sorted(target_identity_ids), current_generation_id),
        ).fetchall()
        if len(rows) != len(target_identity_ids):
            raise ValueError("Inventory target identity is no longer available.")
        return rows
    titles = {candidate.normalized_title for candidate in cluster if candidate.normalized_title}
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
                and (
                    _opposed_claims(left.text, right.text)
                    or _incompatible_claim_values(left.text, right.text)
                )
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


def _incompatible_claim_values(left: str, right: str) -> bool:
    if not _CONFLICT_VALUE_CONTEXT.search(left) or not _CONFLICT_VALUE_CONTEXT.search(right):
        return False
    left_values = tuple(value.casefold() for value in _CONFLICT_VALUE.findall(left))
    right_values = tuple(value.casefold() for value in _CONFLICT_VALUE.findall(right))
    if not left_values or not right_values or left_values == right_values:
        return False
    left_skeleton = _normalized_text(_CONFLICT_VALUE.sub(" VALUE ", left))
    right_skeleton = _normalized_text(_CONFLICT_VALUE.sub(" VALUE ", right))
    return bool(left_skeleton and left_skeleton == right_skeleton)


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
    canonical_title = next(
        (
            candidate.title
            for candidate in cluster
            if candidate.normalized_title == canonical_normalized_title
        ),
        canonical_normalized_title,
    )

    def supported(left: str, right: str) -> bool:
        return any(
            claim_explicitly_supports_alias(left, right, claim.text)
            for candidate in cluster
            for claim in candidate.claims
        )

    for candidate in cluster:
        values = (
            (candidate.title, supported(canonical_title, candidate.title)),
            *(
                (
                    value,
                    supported(candidate.title, value) or supported(canonical_title, value),
                )
                for value in candidate.aliases
            ),
        )
        for value, evidence_supported in values:
            alias, normalized = normalize_knowledge_title(value)
            if evidence_supported and normalized and normalized != canonical_normalized_title:
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


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _applicability_pairs(value: str) -> tuple[tuple[str, str], ...]:
    return applicability_pairs(value)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
