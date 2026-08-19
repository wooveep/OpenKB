"""Review queue for valid Knowledge Analysis claims that lack an Evidence binding."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_knowledge_analysis import (
    KnowledgeAnalysisCandidate,
    KnowledgeAnalysisMissingClaim,
    knowledge_analysis_from_checkpoint,
)
from openkb.desktop_knowledge_analysis_reuse import canonical_analysis_document_id_in
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationSource,
    current_generation_id_in,
    knowledge_content_sha256,
)
from openkb.desktop_knowledge_metadata import encode_knowledge_labels
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_knowledge_reconciliation_changes import IncomingKnowledgeChange
from openkb.desktop_knowledge_sources import (
    available_source_in,
    bind_source_in,
    copy_revision_sources_to_draft_in,
    stable_source_id,
    validate_source_claim_selection,
    validate_unbound_source_claim,
)
from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    stage_okf_projection_in,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock, kb_read_lock

logger = logging.getLogger(__name__)

MissingSourceReason = Literal["source_not_provided", "source_reference_unresolved"]
MissingSourceOutcome = Literal[
    "working_draft", "generated", "review_required", "deduplicated", "dismissed"
]
_MAX_BATCH_DISMISS = 200


@dataclass(frozen=True)
class DesktopMissingSourceCandidate:
    candidate_id: str
    document_id: str
    document_name: str
    kind: Literal["concept", "entity"]
    title: str
    claim_text: str
    reason: MissingSourceReason
    section: str
    locator: dict[str, object]
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "category": "missing_source",
            "document_id": self.document_id,
            "document_name": self.document_name,
            "kind": self.kind,
            "title": self.title,
            "claim_text": self.claim_text,
            "reason": self.reason,
            "section": self.section,
            "locator": self.locator,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class DesktopMissingSourceBinding:
    candidate_id: str
    outcome: MissingSourceOutcome
    remaining_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "decision": "bound",
            "outcome": self.outcome,
            "remaining_count": self.remaining_count,
        }


@dataclass(frozen=True)
class DesktopMissingSourceDismissal:
    resolved_candidate_ids: tuple[str, ...]
    remaining_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": "dismissed",
            "resolved_candidate_ids": list(self.resolved_candidate_ids),
            "remaining_count": self.remaining_count,
        }


def record_missing_source_candidates_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    claims: tuple[KnowledgeAnalysisMissingClaim, ...],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    analysis_provenance_json: str,
) -> None:
    """Persist unresolved claims inside the document publication transaction."""
    if not claims:
        return
    evidence_blocks = dict(evidence)
    now = _timestamp()
    for claim in claims:
        candidate_id = _candidate_id(document_id, claim)
        context = next(
            (
                evidence_blocks[evidence_id]
                for evidence_id in claim.source_evidence_ids
                if evidence_id in evidence_blocks
            ),
            None,
        )
        locator = _block_locator(context) if context is not None else {}
        section = " / ".join(context.heading_path) if context is not None else ""
        connection.execute(
            """
            INSERT INTO knowledge_missing_source_candidates (
                candidate_id, document_id, kind, title, normalized_title,
                entity_subtype, aliases_json, tags_json, claim_text, reason,
                section, locator_json, analysis_provenance_json, created_at
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM knowledge_missing_source_resolution_records
                WHERE candidate_id = ?
            )
            ON CONFLICT(candidate_id) DO UPDATE SET
                entity_subtype = excluded.entity_subtype,
                aliases_json = excluded.aliases_json,
                tags_json = excluded.tags_json,
                reason = excluded.reason,
                section = excluded.section,
                locator_json = excluded.locator_json,
                analysis_provenance_json = excluded.analysis_provenance_json
            """,
            (
                candidate_id,
                document_id,
                claim.kind,
                claim.title,
                claim.normalized_title,
                claim.entity_subtype,
                encode_knowledge_labels(claim.aliases),
                encode_knowledge_labels(claim.tags),
                claim.claim_text,
                claim.reason,
                section,
                json.dumps(locator, ensure_ascii=False, sort_keys=True),
                analysis_provenance_json,
                now,
                candidate_id,
            ),
        )


class DesktopMissingSourceService:
    """Bind or destructively dismiss missing-source review work."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._state_dir = desktop_state_dir(self._kb_dir)
        self._reconciliation = DesktopKnowledgeReconciliationService(self._kb_dir)

    def list_candidates(self) -> tuple[DesktopMissingSourceCandidate, ...]:
        self._require_database()
        with kb_read_lock(self._state_dir):
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT candidates.candidate_id, candidates.document_id,
                        documents.display_name, candidates.kind, candidates.title,
                        candidates.claim_text, candidates.reason, candidates.section,
                        candidates.locator_json, candidates.created_at
                    FROM knowledge_missing_source_candidates AS candidates
                    JOIN source_documents AS documents
                        ON documents.document_id = candidates.document_id
                    ORDER BY candidates.created_at, candidates.candidate_id
                    """
                ).fetchall()
                return tuple(_candidate_from_row(row) for row in rows)
            finally:
                connection.close()

    def bind(self, candidate_id: str, evidence_id: str) -> DesktopMissingSourceBinding:
        self._require_database()
        staged_projection: Path | None = None
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                candidate = _candidate_payload_in(connection, candidate_id)
                _require_bindable_claim(str(candidate[8]))
                try:
                    source = available_source_in(connection, evidence_id)
                except ValueError as error:
                    raise DesktopImportError(
                        "knowledge_source_unavailable",
                        "The selected original Evidence is not currently available.",
                    ) from error
                now = _timestamp()
                outcome = _bind_to_matching_draft_in(
                    connection,
                    candidate=candidate,
                    evidence_id=evidence_id,
                    updated_at=now,
                )
                if outcome is None:
                    outcome = _bind_to_matching_revision_draft_in(
                        connection,
                        candidate=candidate,
                        evidence_id=evidence_id,
                        updated_at=now,
                    )
                if outcome is None:
                    initial_generation_id = current_generation_id_in(connection)
                    conflicts = self._reconciliation.record_analysis_changes_in(
                        connection,
                        source.document_id,
                        (_bound_change(candidate, evidence_id),),
                    )
                    current_generation_id = current_generation_id_in(connection)
                    if conflicts:
                        outcome = "review_required"
                    elif current_generation_id != initial_generation_id:
                        outcome = "generated"
                        staged_projection = stage_okf_projection_in(connection, self._kb_dir)
                    else:
                        outcome = "deduplicated"
                _record_resolution_in(
                    connection,
                    candidate_id=candidate_id,
                    document_id=str(candidate[1]),
                    decision="bound",
                    evidence_id=evidence_id,
                    outcome=outcome,
                    resolved_at=now,
                )
                connection.execute(
                    "DELETE FROM knowledge_missing_source_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                )
                remaining = _remaining_count_in(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                if staged_projection is not None:
                    discard_okf_projection_staging(staged_projection)
                raise
            finally:
                connection.close()
            if staged_projection is not None:
                try:
                    activate_okf_projection(self._kb_dir, staged_projection)
                except Exception:
                    logger.exception("Could not activate Missing Source binding OKF projection.")
                finally:
                    discard_okf_projection_staging(staged_projection)
        return DesktopMissingSourceBinding(candidate_id, outcome, remaining)

    def dismiss(self, candidate_ids: tuple[str, ...]) -> DesktopMissingSourceDismissal:
        selected = tuple(dict.fromkeys(candidate_ids))
        if not selected or len(selected) > _MAX_BATCH_DISMISS:
            raise DesktopImportError(
                "missing_source_candidates_invalid",
                f"Choose between 1 and {_MAX_BATCH_DISMISS} Missing Source Candidates.",
            )
        self._require_database()
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in selected)
                rows = connection.execute(
                    f"""SELECT candidate_id, document_id, kind, normalized_title, claim_text
                    FROM knowledge_missing_source_candidates
                    WHERE candidate_id IN ({placeholders})""",
                    selected,
                ).fetchall()
                documents = {str(row[0]): str(row[1]) for row in rows}
                if set(documents) != set(selected):
                    raise DesktopImportError(
                        "missing_source_candidate_not_found",
                        "One or more Missing Source Candidates are no longer pending.",
                    )
                _redact_dismissed_claims_in(connection, rows)
                now = _timestamp()
                for candidate_id in selected:
                    _record_resolution_in(
                        connection,
                        candidate_id=candidate_id,
                        document_id=documents[candidate_id],
                        decision="dismissed",
                        evidence_id=None,
                        outcome="dismissed",
                        resolved_at=now,
                    )
                connection.execute(
                    f"DELETE FROM knowledge_missing_source_candidates "
                    f"WHERE candidate_id IN ({placeholders})",
                    selected,
                )
                remaining = _remaining_count_in(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return DesktopMissingSourceDismissal(selected, remaining)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _require_database(self) -> None:
        if not self._database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                "Open a Desktop Knowledge Base before reviewing Missing Source Candidates.",
            )


def _redact_dismissed_claims_in(
    connection: sqlite3.Connection, rows: list[tuple[object, ...]]
) -> None:
    """Erase dismissed claim bodies from their canonical analysis checkpoints."""
    removals: dict[str, set[tuple[str, str, str]]] = {}
    for row in rows:
        canonical_document_id = canonical_analysis_document_id_in(connection, str(row[1]))
        removals.setdefault(canonical_document_id, set()).add(
            (str(row[2]), str(row[3]), str(row[4]))
        )

    for document_id, identities in removals.items():
        checkpoint_row = connection.execute(
            """
            SELECT runtime.stage_run_id, runtime.checkpoint_json
            FROM import_jobs AS jobs
            JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
                AND stages.stage = 'model_analysis' AND stages.status = 'completed'
            JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
            WHERE jobs.document_id = ? AND runtime.checkpoint_json IS NOT NULL
            ORDER BY jobs.completed_at DESC, jobs.created_at DESC LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        if checkpoint_row is None:
            raise DesktopImportError(
                "import_checkpoint_invalid",
                "The Missing Source Candidate analysis checkpoint is unavailable.",
            )
        try:
            checkpoint = json.loads(str(checkpoint_row[1]))
        except json.JSONDecodeError as error:
            raise DesktopImportError(
                "import_checkpoint_invalid",
                "The Missing Source Candidate analysis checkpoint is invalid.",
            ) from error
        analysis = knowledge_analysis_from_checkpoint(checkpoint)
        if analysis is None:
            raise DesktopImportError(
                "import_checkpoint_invalid",
                "The Missing Source Candidate analysis checkpoint is invalid.",
            )

        def retained_candidates(
            candidates: tuple[KnowledgeAnalysisCandidate, ...],
        ) -> tuple[KnowledgeAnalysisCandidate, ...]:
            retained: list[KnowledgeAnalysisCandidate] = []
            for candidate in candidates:
                normalized_title = normalize_knowledge_title(candidate.title)[1]
                claims = []
                for claim in candidate.claims:
                    identity = (candidate.kind, normalized_title, claim.text)
                    if identity in identities:
                        continue
                    claims.append(claim)
                if claims:
                    retained.append(replace(candidate, claims=tuple(claims)))
            return tuple(retained)

        redacted = replace(
            analysis,
            concepts=retained_candidates(analysis.concepts),
            entities=retained_candidates(analysis.entities),
        )
        # D1 document versions can share this checkpoint. An earlier dismissal
        # may therefore have already removed the same claim identity.
        checkpoint["normalized_result"] = redacted.as_dict()
        connection.execute(
            """
            UPDATE stage_run_runtime SET checkpoint_json = ?, updated_at = ?
            WHERE stage_run_id = ?
            """,
            (
                json.dumps(checkpoint, ensure_ascii=False),
                _timestamp(),
                str(checkpoint_row[0]),
            ),
        )


def _candidate_payload_in(connection: sqlite3.Connection, candidate_id: str) -> tuple[object, ...]:
    row = connection.execute(
        """
        SELECT candidate_id, document_id, kind, title, normalized_title, entity_subtype,
            aliases_json, tags_json, claim_text, analysis_provenance_json
        FROM knowledge_missing_source_candidates WHERE candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise DesktopImportError(
            "missing_source_candidate_not_found",
            "The selected Missing Source Candidate is no longer pending.",
        )
    return tuple(row)


def _bound_change(candidate: tuple[object, ...], evidence_id: str) -> IncomingKnowledgeChange:
    from openkb.desktop_knowledge_metadata import decode_knowledge_labels

    claim_text = str(candidate[8])
    source_id = stable_source_id(evidence_id)
    content_markdown = f"{claim_text}[^{source_id}]"
    return IncomingKnowledgeChange(
        source_block_id=None,
        kind=str(candidate[2]),
        is_kind_explicit=True,
        title=str(candidate[3]),
        normalized_title=str(candidate[4]),
        content_markdown=content_markdown,
        content_sha256=knowledge_content_sha256(content_markdown),
        entity_subtype=str(candidate[5]) if candidate[5] is not None else None,
        aliases=decode_knowledge_labels(candidate[6]),
        tags=decode_knowledge_labels(candidate[7]),
        sources=(KnowledgeGenerationSource(source_id, evidence_id, claim_text),),
        analysis_provenance_json=str(candidate[9]),
    )


def _require_bindable_claim(claim_text: str) -> None:
    try:
        validate_unbound_source_claim(claim_text)
    except ValueError as error:
        raise DesktopImportError(
            "knowledge_source_claim_invalid",
            "The Missing Source claim contains unsupported Markdown or source markers.",
        ) from error


def _bind_to_matching_draft_in(
    connection: sqlite3.Connection,
    *,
    candidate: tuple[object, ...],
    evidence_id: str,
    updated_at: str,
) -> MissingSourceOutcome | None:
    row = connection.execute(
        """
        SELECT page_id FROM knowledge_page_working_drafts
        WHERE kind = ? AND normalized_title = ?
        ORDER BY updated_at DESC, page_id LIMIT 1
        """,
        (str(candidate[2]), str(candidate[4])),
    ).fetchone()
    if row is None:
        return None
    try:
        bind_source_in(
            connection,
            page_id=str(row[0]),
            claim_text=str(candidate[8]),
            evidence_id=evidence_id,
            updated_at=updated_at,
        )
    except ValueError as error:
        if str(error) in {
            "knowledge_claim_selection_invalid",
            "knowledge_claim_selection_structural",
        }:
            return None
        raise DesktopImportError("knowledge_source_binding_failed", str(error)) from error
    return "working_draft"


def _bind_to_matching_revision_draft_in(
    connection: sqlite3.Connection,
    *,
    candidate: tuple[object, ...],
    evidence_id: str,
    updated_at: str,
) -> MissingSourceOutcome | None:
    """Create an editable Draft when the claim already exists in a user revision."""
    row = connection.execute(
        """
        SELECT pages.page_id, pages.kind, revisions.title, pages.normalized_title,
            revisions.content_markdown
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        WHERE pages.kind = ? AND pages.normalized_title = ?
            AND NOT EXISTS (
                SELECT 1 FROM knowledge_page_working_drafts AS drafts
                WHERE drafts.page_id = pages.page_id
            )
        """,
        (str(candidate[2]), str(candidate[4])),
    ).fetchone()
    if row is None:
        return None
    claim_text = str(candidate[8])
    try:
        validate_source_claim_selection(str(row[4]), claim_text)
    except ValueError as error:
        if str(error) in {
            "knowledge_claim_selection_invalid",
            "knowledge_claim_selection_structural",
        }:
            return None
        raise
    page_id = str(row[0])
    connection.execute(
        """
        INSERT INTO knowledge_page_working_drafts (
            page_id, kind, title, normalized_title, content_markdown,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (page_id, str(row[1]), str(row[2]), str(row[3]), str(row[4]), updated_at, updated_at),
    )
    copy_revision_sources_to_draft_in(connection, page_id, updated_at)
    try:
        bind_source_in(
            connection,
            page_id=page_id,
            claim_text=claim_text,
            evidence_id=evidence_id,
            updated_at=updated_at,
        )
    except ValueError as error:
        raise DesktopImportError("knowledge_source_binding_failed", str(error)) from error
    return "working_draft"


def _record_resolution_in(
    connection: sqlite3.Connection,
    *,
    candidate_id: str,
    document_id: str,
    decision: Literal["bound", "dismissed"],
    evidence_id: str | None,
    outcome: MissingSourceOutcome,
    resolved_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_missing_source_resolution_records (
            resolution_id, candidate_id, document_id, decision, evidence_id,
            outcome, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            candidate_id,
            document_id,
            decision,
            evidence_id,
            outcome,
            resolved_at,
        ),
    )


def _candidate_from_row(row: tuple[object, ...]) -> DesktopMissingSourceCandidate:
    try:
        locator = json.loads(str(row[8]))
    except json.JSONDecodeError as error:
        raise DesktopImportError(
            "desktop_import_state_invalid", "Missing Source Candidate locator is invalid."
        ) from error
    if not isinstance(locator, dict):
        raise DesktopImportError(
            "desktop_import_state_invalid", "Missing Source Candidate locator is invalid."
        )
    return DesktopMissingSourceCandidate(
        candidate_id=str(row[0]),
        document_id=str(row[1]),
        document_name=str(row[2]),
        kind=str(row[3]),  # type: ignore[arg-type]
        title=str(row[4]),
        claim_text=str(row[5]),
        reason=str(row[6]),  # type: ignore[arg-type]
        section=str(row[7]),
        locator=locator,
        created_at=str(row[9]),
    )


def _candidate_id(document_id: str, claim: KnowledgeAnalysisMissingClaim) -> str:
    identity = "\0".join(
        (document_id, claim.kind, claim.normalized_title, claim.claim_text)
    ).encode("utf-8")
    return f"msc-{hashlib.sha256(identity).hexdigest()[:24]}"


def _block_locator(block: DocumentIRBlock) -> dict[str, object]:
    return block.locator or {
        "line_start": block.line_start,
        "line_end": block.line_end,
        "heading_path": list(block.heading_path),
    }


def _remaining_count_in(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM knowledge_missing_source_candidates").fetchone()
    return int(row[0])


def _timestamp() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()
