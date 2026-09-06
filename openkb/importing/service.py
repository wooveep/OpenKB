"""Public Desktop document import API.

The worker, persistence state machine, artifacts, and wire values live in
focused modules so later format adapters can reuse the same Stage Run runtime.
"""

from __future__ import annotations

from openkb.importing.artifacts import DesktopImportError, DocumentIRBlock
from openkb.importing.runner import DesktopImportControl, DesktopTextImportService
from openkb.importing.sources import DesktopImportSource, DesktopImportSourceInspection
from openkb.importing.store import IMPORT_STAGES
from openkb.importing.types import (
    DesktopDeduplication,
    DesktopDocumentVersionCandidate,
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
    "DesktopDeduplication",
    "DesktopDocumentVersionCandidate",
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
