"""Public Desktop TXT import API.

The worker, persistence state machine, artifacts, and wire values live in
focused modules so later format adapters can reuse the same Stage Run runtime.
"""

from __future__ import annotations

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_import_runner import DesktopImportControl, DesktopTextImportService
from openkb.desktop_import_sources import DesktopImportSource, DesktopImportSourceInspection
from openkb.desktop_import_store import IMPORT_STAGES
from openkb.desktop_import_types import (
    DesktopImportedDocument,
    DesktopImportJob,
    DesktopImportTask,
    DesktopModelAttempt,
    DesktopModelCall,
    DesktopQuarantinedDocument,
    DesktopRecoveryOverride,
    DesktopStageRun,
    DesktopTextImportResult,
)

_STAGES = IMPORT_STAGES

__all__ = [
    "DesktopImportControl",
    "DesktopImportError",
    "DesktopImportedDocument",
    "DesktopImportJob",
    "DesktopImportSource",
    "DesktopImportSourceInspection",
    "DesktopImportTask",
    "DesktopModelAttempt",
    "DesktopModelCall",
    "DesktopQuarantinedDocument",
    "DesktopRecoveryOverride",
    "DesktopStageRun",
    "DesktopTextImportResult",
    "DesktopTextImportService",
    "DocumentIRBlock",
]
