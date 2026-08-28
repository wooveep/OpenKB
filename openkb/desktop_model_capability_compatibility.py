"""Provider-free compatibility for legacy operation-coupled Analysis evidence."""

from __future__ import annotations

import json
import sqlite3

from openkb.desktop_model_execution_profile import DesktopModelExecutionProfile


def migrate_legacy_analysis_capability_profiles_in(
    connection: sqlite3.Connection,
    *,
    migrated_at: str,
) -> None:
    """Move only durable legacy successes; ambiguous invalidations stay unverified."""
    rows = connection.execute(
        """
        SELECT profile_identity, profile_json, status, failure_code, reason,
            checked_at, created_at, updated_at
        FROM model_capability_checks
        ORDER BY updated_at, profile_identity
        """
    ).fetchall()
    for row in rows:
        legacy_identity = str(row[0])
        try:
            raw_profile = json.loads(str(row[1]))
            if not isinstance(raw_profile, dict) or raw_profile.get("role") is not None:
                continue
            legacy = DesktopModelExecutionProfile.from_dict(raw_profile)
        except (json.JSONDecodeError, TypeError, ValueError):
            _audit(
                connection,
                legacy_identity=legacy_identity,
                shared_identity=None,
                decision="invalid_legacy_profile",
                evidence={"legacy_profile_parseable": False},
                migrated_at=migrated_at,
            )
            continue

        shared = legacy.capability_evidence_profile
        legacy_status = str(row[2])
        checked_at: str | None
        if legacy_status == "verified":
            status = "verified"
            failure_code = None
            reason = None
            checked_at = str(row[5]) if row[5] is not None else str(row[7])
            decision = "carried_verified"
        else:
            status = "unchecked"
            failure_code = None
            reason = None
            checked_at = None
            decision = "left_unverified"

        connection.execute(
            """
            INSERT INTO model_capability_checks (
                profile_identity, profile_json, status, failure_code, reason,
                checked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_identity) DO UPDATE SET
                profile_json = excluded.profile_json,
                status = excluded.status,
                failure_code = excluded.failure_code,
                reason = excluded.reason,
                checked_at = excluded.checked_at,
                updated_at = excluded.updated_at
            """,
            (
                shared.identity,
                _json(shared.as_dict()),
                status,
                failure_code,
                reason,
                checked_at,
                str(row[6]),
                migrated_at,
            ),
        )
        _audit(
            connection,
            legacy_identity=legacy_identity,
            shared_identity=shared.identity,
            decision=decision,
            evidence={
                "legacy_status": legacy_status,
                "legacy_failure_code": row[3],
                "ambiguous_failure_not_mapped_to_current_contract": legacy_status != "verified",
                "provider_called": False,
            },
            migrated_at=migrated_at,
        )


def _audit(
    connection: sqlite3.Connection,
    *,
    legacy_identity: str,
    shared_identity: str | None,
    decision: str,
    evidence: dict[str, object],
    migrated_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO model_capability_compatibility_audit (
            legacy_profile_identity, shared_profile_identity, decision,
            evidence_json, migrated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(legacy_profile_identity) DO NOTHING
        """,
        (
            legacy_identity,
            shared_identity,
            decision,
            _json(evidence),
            migrated_at,
        ),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
