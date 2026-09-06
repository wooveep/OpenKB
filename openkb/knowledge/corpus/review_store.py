"""Evidence-bound semantic review snapshots and reusable human decisions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openkb.knowledge.corpus.candidates import CorpusCandidate
from openkb.knowledge.corpus.work_queue import enqueue_corpus_work_in
from openkb.locks import kb_ingest_lock
from openkb.shared.canonical_json import canonical_json, canonical_json_digest
from openkb.shared.clock import timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

SEMANTIC_REVIEW_MIGRATION_STATEMENTS = (
    "ALTER TABLE knowledge_identity_review_items "
    "ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE knowledge_identity_review_items ADD COLUMN decision TEXT",
    "ALTER TABLE knowledge_identity_review_items ADD COLUMN authority TEXT",
    "ALTER TABLE knowledge_identity_review_items "
    "ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}'",
)


def review_snapshot(cluster: tuple[CorpusCandidate, ...], reason: str) -> dict[str, object]:
    return {
        "reason": reason,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "document_id": candidate.document_id,
                "candidate_generation_id": candidate.candidate_generation_id,
                "title": candidate.title,
                "kind": candidate.kind,
                "aliases": list(candidate.aliases),
                "claims": [asdict(claim) for claim in candidate.claims],
            }
            for candidate in sorted(
                cluster, key=lambda c: (c.candidate_generation_id, c.candidate_id)
            )
        ],
    }


def review_decision_in(
    db: sqlite3.Connection, cluster: tuple[CorpusCandidate, ...], reason: str
) -> str | None:
    row = db.execute(
        "SELECT decision FROM knowledge_identity_review_items WHERE review_id = ?",
        (canonical_json_digest(review_snapshot(cluster, reason)),),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def record_review_in(
    db: sqlite3.Connection, cluster: tuple[CorpusCandidate, ...], reason: str, now: str
) -> str:
    snapshot = review_snapshot(cluster, reason)
    review_id = canonical_json_digest(snapshot)
    db.execute(
        "INSERT INTO knowledge_identity_review_items "
        "(review_id, kind, reason, candidate_ids_json, status, created_at, snapshot_json) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?) ON CONFLICT(review_id) DO NOTHING",
        (
            review_id,
            cluster[0].kind,
            reason,
            canonical_json(sorted(c.candidate_id for c in cluster)),
            now,
            canonical_json(snapshot),
        ),
    )
    return review_id


def has_nonliteral_cross_document_claims(cluster: tuple[CorpusCandidate, ...]) -> bool:
    """Detect a comparison need without asserting any semantic relationship."""
    if len({candidate.document_id for candidate in cluster}) < 2:
        return False
    claims = {
        (claim.applicability, " ".join(claim.text.split()).casefold())
        for candidate in cluster
        for claim in candidate.claims
    }
    return len(claims) > 1


def snapshot_is_current_in(db: sqlite3.Connection, snapshot: dict) -> bool:
    candidates = snapshot.get("candidates")
    return (
        isinstance(candidates, list)
        and bool(candidates)
        and all(
            db.execute(
                "SELECT 1 FROM knowledge_candidate_registry_state AS registry "
                "JOIN source_documents AS documents "
                "ON documents.document_id = registry.document_id "
                "WHERE registry.document_id = ? AND current_candidate_generation_id = ? "
                "AND documents.availability = 'available'",
                (c["document_id"], c["candidate_generation_id"]),
            ).fetchone()
            for c in candidates
        )
    )


def candidate_kept_separate_in(db: sqlite3.Connection, candidate: CorpusCandidate) -> bool:
    rows = db.execute(
        "SELECT snapshot_json FROM knowledge_identity_review_items "
        "WHERE decision = 'keep_separate' AND authority = 'human'"
    ).fetchall()
    for row in rows:
        snapshot = json.loads(row[0])
        if snapshot_is_current_in(db, snapshot) and any(
            value["candidate_id"] == candidate.candidate_id
            and value["candidate_generation_id"] == candidate.candidate_generation_id
            for value in snapshot["candidates"]
        ):
            return True
    return False


def review_choices(reason: str) -> tuple[str, ...]:
    if reason == "claim_relationship_review":
        return ("compatible", "keep_current")
    if reason == "semantic_identity_confirmation_required":
        return ("same_identity", "keep_separate", "keep_current")
    return ("keep_current",)


class CorpusReviewService:
    def __init__(self, kb_dir: Path):
        self._kb_dir = kb_dir

    def list_items(self) -> list[dict[str, Any]]:
        with closing(connect_database(desktop_state_database_path(self._kb_dir))) as db:
            rows = db.execute(
                "SELECT review_id, reason, status, snapshot_json, decision, authority "
                "FROM knowledge_identity_review_items ORDER BY created_at, review_id"
            ).fetchall()
            result = []
            for review_id, reason, status, serialized, decision, authority in rows:
                snapshot = json.loads(serialized)
                if not snapshot_is_current_in(db, snapshot):
                    continue
                evidence_ids = sorted(
                    {
                        eid
                        for c in snapshot["candidates"]
                        for claim in c["claims"]
                        for eid in claim["evidence_ids"]
                    }
                )
                evidence = []
                for eid in evidence_ids:
                    row = db.execute(
                        "SELECT text FROM evidence_refs WHERE evidence_id = ?", (eid,)
                    ).fetchone()
                    if row:
                        evidence.append({"evidence_id": eid, "text": row[0]})
                result.append(
                    {
                        "review_id": review_id,
                        "reason": reason,
                        "status": status,
                        "candidates": snapshot["candidates"],
                        "evidence": evidence,
                        "decision": decision,
                        "authority": authority,
                        "choices": list(review_choices(reason)),
                    }
                )
            return result

    def resolve(self, review_id: str, decision: str) -> None:
        with (
            kb_ingest_lock(desktop_state_dir(self._kb_dir)),
            closing(connect_database(desktop_state_database_path(self._kb_dir))) as db,
        ):
            with db:
                row = db.execute(
                    "SELECT reason, snapshot_json FROM knowledge_identity_review_items "
                    "WHERE review_id = ?",
                    (review_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("semantic_review_not_found")
                snapshot = json.loads(row[1])
                if not snapshot_is_current_in(db, snapshot):
                    raise ValueError("semantic_review_snapshot_superseded")
                allowed = review_choices(row[0])
                if decision not in allowed:
                    raise ValueError("semantic_review_decision_invalid")
                db.execute(
                    "UPDATE knowledge_identity_review_items SET status = 'resolved', "
                    "decision = ?, authority = 'human', resolved_at = ? WHERE review_id = ?",
                    (decision, timestamp(), review_id),
                )
                for candidate in snapshot["candidates"]:
                    enqueue_corpus_work_in(db, candidate["document_id"])
