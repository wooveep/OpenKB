"""Knowledge Graph requeueing after atomic corpus Reanalysis activation."""

from __future__ import annotations

import logging
from pathlib import Path

from openkb.knowledge.graph.tasks import DesktopKnowledgeGraphExtractionTasks
from openkb.models.gateway import DesktopModelGateway

logger = logging.getLogger(__name__)


def requeue_reanalysis_graphs(
    kb_dir: Path,
    document_ids: tuple[str, ...],
    gateway: DesktopModelGateway,
) -> tuple[str, ...]:
    """Bind durable graph tasks to newly activated candidate registries."""
    queued: list[str] = []
    tasks = DesktopKnowledgeGraphExtractionTasks(kb_dir)
    for document_id in document_ids:
        try:
            if tasks.queue(document_id, gateway):
                queued.append(document_id)
        except Exception:
            logger.exception(
                "Could not requeue Knowledge Graph extraction after Reanalysis for %s.",
                document_id,
            )
    return tuple(queued)
