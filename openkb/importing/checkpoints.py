"""Required-checkpoint boundary for resumable Desktop imports."""

from __future__ import annotations

from collections.abc import Mapping

from openkb.importing.artifacts import DesktopImportError
from openkb.importing.store import IMPORT_STAGES, DesktopImportStore, ImportJobState
from openkb.importing.types import DesktopStageRun


def require_import_checkpoint(
    store: DesktopImportStore,
    state: ImportJobState,
    stage: str,
) -> object:
    """Return one required Stage checkpoint or raise the public state error."""
    checkpoint = store.checkpoint(state.stage_ids[stage])
    if checkpoint is None:
        raise DesktopImportError(
            "import_checkpoint_invalid",
            f"Completed stage {stage} has no usable checkpoint.",
        )
    return checkpoint


def next_import_stage(
    store: DesktopImportStore,
    state: ImportJobState,
    stages: Mapping[str, DesktopStageRun] | None = None,
) -> str:
    """Select the earliest durable Stage that is not complete or skipped."""
    values = stages or {stage.stage: stage for stage in store.stage_runs(state.job_id)}
    return next(
        (stage for stage in IMPORT_STAGES if values[stage].status not in {"completed", "skipped"}),
        "search",
    )
