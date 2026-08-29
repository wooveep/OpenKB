"""Provider-free compatibility for legacy operation-coupled Analysis evidence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from openkb.desktop_model_execution_profile import DesktopModelExecutionProfile

_GRAPH_OPERATION = "knowledge_graph_extraction"
_GRAPH_FAILURE_CODE = "knowledge_graph_response_invalid"
_GRAPH_DIAGNOSTIC_MATCH_SECONDS = 2.0


@dataclass(frozen=True)
class _LegacyCapabilityRow:
    legacy_identity: str
    profile: DesktopModelExecutionProfile
    shared_identity: str
    shared_profile_json: dict[str, object]
    status: str
    failure_code: str | None
    reason: str | None
    checked_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class _GraphLocalEvidence:
    successful_profile_identity: str
    successful_checked_at: str
    diagnostic_id: str
    diagnostic_created_at: str


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
    legacy_rows: list[_LegacyCapabilityRow] = []
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
        legacy_rows.append(
            _LegacyCapabilityRow(
                legacy_identity=legacy_identity,
                profile=legacy,
                shared_identity=shared.identity,
                shared_profile_json=shared.as_dict(),
                status=str(row[2]),
                failure_code=str(row[3]) if row[3] is not None else None,
                reason=str(row[4]) if row[4] is not None else None,
                checked_at=str(row[5]) if row[5] is not None else None,
                created_at=str(row[6]),
                updated_at=str(row[7]),
            )
        )

    verified_by_shared: dict[str, list[_LegacyCapabilityRow]] = {}
    for legacy_row in legacy_rows:
        if legacy_row.status == "verified":
            verified_by_shared.setdefault(legacy_row.shared_identity, []).append(legacy_row)

    for legacy_row in legacy_rows:
        graph_evidence = _proven_graph_local_invalidation_in(
            connection,
            legacy_row,
            verified_by_shared.get(legacy_row.shared_identity, []),
        )
        checked_at: str | None
        if legacy_row.status == "verified":
            status = "verified"
            failure_code = None
            reason = None
            checked_at = legacy_row.checked_at or legacy_row.updated_at
            decision = "carried_verified"
        elif graph_evidence is not None:
            status = "verified"
            failure_code = None
            reason = None
            checked_at = graph_evidence.successful_checked_at
            decision = "restored_graph_local"
            _suspend_legacy_graph_contract_in(
                connection,
                legacy_row,
                migrated_at=migrated_at,
            )
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
                legacy_row.shared_identity,
                _json(legacy_row.shared_profile_json),
                status,
                failure_code,
                reason,
                checked_at,
                legacy_row.created_at,
                migrated_at,
            ),
        )
        _audit(
            connection,
            legacy_identity=legacy_row.legacy_identity,
            shared_identity=legacy_row.shared_identity,
            decision=decision,
            evidence=_audit_evidence(legacy_row, graph_evidence),
            migrated_at=migrated_at,
        )


def _proven_graph_local_invalidation_in(
    connection: sqlite3.Connection,
    invalidated: _LegacyCapabilityRow,
    verified_rows: list[_LegacyCapabilityRow],
) -> _GraphLocalEvidence | None:
    """Require exact shared success plus operation attribution; proximity alone is insufficient."""
    if invalidated.status == "verified":
        return None
    operation_named = (
        invalidated.failure_code == _GRAPH_FAILURE_CODE
        or "knowledge graph" in (invalidated.reason or "").casefold()
    )
    if invalidated.failure_code not in {"model_response_invalid", _GRAPH_FAILURE_CODE}:
        return None
    if not operation_named:
        return None
    invalidated_at = _instant(invalidated.updated_at)
    if invalidated_at is None:
        return None
    successful = [
        row
        for row in verified_rows
        if (checked := _instant(row.checked_at or row.updated_at)) is not None
        and checked <= invalidated_at
    ]
    if not successful:
        return None
    successful_row = max(
        successful,
        key=lambda row: _instant(row.checked_at or row.updated_at) or datetime.min,
    )
    diagnostics = connection.execute(
        """
        SELECT diagnostic_id, created_at
        FROM knowledge_graph_diagnostics
        WHERE phase = 'extraction' AND error_code = ?
        ORDER BY created_at, diagnostic_id
        """,
        (_GRAPH_FAILURE_CODE,),
    ).fetchall()
    matching = [
        (str(row[0]), str(row[1]), observed)
        for row in diagnostics
        if (observed := _instant(str(row[1]))) is not None
        and abs((observed - invalidated_at).total_seconds()) <= _GRAPH_DIAGNOSTIC_MATCH_SECONDS
    ]
    if not matching:
        return None
    diagnostic_id, diagnostic_created_at, _observed = min(
        matching,
        key=lambda item: abs((item[2] - invalidated_at).total_seconds()),
    )
    return _GraphLocalEvidence(
        successful_profile_identity=successful_row.legacy_identity,
        successful_checked_at=successful_row.checked_at or successful_row.updated_at,
        diagnostic_id=diagnostic_id,
        diagnostic_created_at=diagnostic_created_at,
    )


def _suspend_legacy_graph_contract_in(
    connection: sqlite3.Connection,
    legacy_row: _LegacyCapabilityRow,
    *,
    migrated_at: str,
) -> None:
    """Retain the old aggregate contract identity without suspending the upgraded graph contract."""
    reason = "Legacy Knowledge Graph output was invalid; retry its replacement explicitly."
    connection.execute(
        """
        INSERT INTO model_operation_contract_states (
            operation, capability_identity, prompt_contract_digest, status,
            failure_code, reason, failure_stage, failure_signature,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'suspended', ?, ?, 'domain_validation', NULL, ?, ?)
        ON CONFLICT(operation, capability_identity, prompt_contract_digest) DO NOTHING
        """,
        (
            _GRAPH_OPERATION,
            legacy_row.shared_identity,
            legacy_row.profile.prompt_contract_digest,
            _GRAPH_FAILURE_CODE,
            reason,
            legacy_row.updated_at,
            migrated_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_operation_contract_events (
            operation, capability_identity, prompt_contract_digest, status,
            failure_code, failure_stage, failure_signature, created_at
        ) VALUES (?, ?, ?, 'suspended', ?, 'domain_validation', NULL, ?)
        """,
        (
            _GRAPH_OPERATION,
            legacy_row.shared_identity,
            legacy_row.profile.prompt_contract_digest,
            _GRAPH_FAILURE_CODE,
            migrated_at,
        ),
    )


def _audit_evidence(
    legacy_row: _LegacyCapabilityRow,
    graph_evidence: _GraphLocalEvidence | None,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "legacy_status": legacy_row.status,
        "legacy_failure_code": legacy_row.failure_code,
        "provider_called": False,
    }
    if graph_evidence is None:
        evidence["ambiguous_failure_not_mapped_to_current_contract"] = (
            legacy_row.status != "verified"
        )
        return evidence
    evidence.update(
        {
            "successful_profile_identity": graph_evidence.successful_profile_identity,
            "successful_checked_at": graph_evidence.successful_checked_at,
            "graph_diagnostic_id": graph_evidence.diagnostic_id,
            "graph_diagnostic_created_at": graph_evidence.diagnostic_created_at,
            "mapped_operation": _GRAPH_OPERATION,
            "mapped_legacy_contract_digest": legacy_row.profile.prompt_contract_digest,
        }
    )
    return evidence


def _instant(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
