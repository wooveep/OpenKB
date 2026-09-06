"""Transaction-aware persistence for Knowledge Identity candidate bindings."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

IdentityCandidateBinding = tuple[str, str, str]


def bind_identity_candidates_in(
    connection: sqlite3.Connection,
    bindings: Iterable[IdentityCandidateBinding],
    *,
    now: str,
) -> None:
    """Upsert identity-to-candidate bindings inside the caller's transaction."""
    connection.executemany(
        """
        INSERT INTO knowledge_identity_candidates (
            identity_id, candidate_id, match_basis, created_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(identity_id, candidate_id) DO UPDATE SET
            match_basis = excluded.match_basis,
            created_at = excluded.created_at
        """,
        ((*binding, now) for binding in bindings),
    )
