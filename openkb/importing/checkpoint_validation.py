"""Small pure predicates for Desktop import checkpoint selection."""

from __future__ import annotations

from collections.abc import Mapping

from openkb.importing.types import DesktopStageRun


def stage_completed(stages: Mapping[str, DesktopStageRun], stage: str) -> bool:
    return stages[stage].status in {"completed", "skipped"}


def matches_preflight_checkpoint(checkpoint: object, asset_sha256: str, raw_size: int) -> bool:
    return (
        isinstance(checkpoint, dict)
        and checkpoint.get("asset_sha256") == asset_sha256
        and type(checkpoint.get("raw_size")) is int
        and checkpoint["raw_size"] == raw_size
    )
