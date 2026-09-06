"""Create/open Desktop Knowledge Bases with ordered runtime recovery."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.answers.conversations import recover_stale_conversation_generations
from openkb.documents.raw_assets import DesktopRawAssetService
from openkb.engine import knowledge_graph as graph_engine
from openkb.engine import knowledge_reanalysis as reanalysis_engine
from openkb.engine import page_tree_enrichment as enrichment_engine
from openkb.knowledge.graph.tasks import DesktopKnowledgeGraphExtractionTasks
from openkb.knowledge.pages.okf_projection import (
    has_valid_okf_projection,
    materialize_okf_projection,
)
from openkb.knowledge.reanalysis import service as reanalysis_runtime
from openkb.page_tree.enrichment import DesktopPageTreeEnrichmentService
from openkb.page_tree.store import start_page_tree_rebuilds
from openkb.retrieval.catalog_store import start_catalog_rebuilds

if TYPE_CHECKING:
    from openkb.engine.protocol import DesktopRequest
    from openkb.engine.server import DesktopEngineServer


def dispatch_knowledge_base_activation(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Change the active workspace only after invalidating old background work."""
    from openkb.engine.protocol import DesktopRequestError, required_string_param

    kb_dir = Path(required_string_param(request, "kb_dir"))
    name_value = request.params.get("name")
    if request.method == "workbench.create_knowledge_base" and (
        name_value is not None and not isinstance(name_value, str)
    ):
        raise DesktopRequestError(
            "invalid_params", "workbench.create_knowledge_base name must be a string."
        )
    with server._workspace_transition.activation(kb_dir):
        with server._workspace_requests_lock:
            return _activate_knowledge_base(server, request, cancel_event, kb_dir, name_value)


def _activate_knowledge_base(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
    kb_dir: Path,
    name_value: object,
) -> dict[str, object]:
    previous = server._workspace.active()
    try:
        server._begin_workspace_mutation(request, cancel_event)
        _interrupt_previous_reanalysis(server)
        if request.method == "workbench.create_knowledge_base":
            name = name_value if isinstance(name_value, str) else None
            activation = server._workspace.create(kb_dir, name=name)
            materialize_okf_projection(Path(activation.knowledge_base.kb_dir))
            return activation.as_dict()

        activation = server._workspace.open(kb_dir)
        active_kb_dir = Path(activation.knowledge_base.kb_dir)
        recover_stale_conversation_generations(active_kb_dir)
        reanalysis_runtime.recover_interrupted_knowledge_reanalysis(active_kb_dir)
        DesktopRawAssetService(active_kb_dir).verify_available_documents()
        _materialize_okf_projection_on_open(active_kb_dir)
        start_page_tree_rebuilds(active_kb_dir)
        start_catalog_rebuilds(active_kb_dir, recover=True)
        # Runtime restoration may repair durable state, but it must never start
        # provider work. Imports and enrichment resume only after a user action.
        DesktopPageTreeEnrichmentService(active_kb_dir).recover_interrupted()
        DesktopKnowledgeGraphExtractionTasks(active_kb_dir).recover_interrupted()
        return activation.as_dict()
    except BaseException:
        server._workspace.restore_active(previous)
        raise


def _interrupt_previous_reanalysis(server: DesktopEngineServer) -> None:
    previous = server._workspace.active()
    reanalysis_engine.invalidate_knowledge_reanalysis_workers(server)
    enrichment_engine.invalidate_page_tree_enrichment_workers(server)
    graph_engine.invalidate_knowledge_graph_workers(server)
    if previous is not None:
        reanalysis_runtime.recover_interrupted_knowledge_reanalysis(Path(previous.kb_dir))
        DesktopPageTreeEnrichmentService(Path(previous.kb_dir)).recover_interrupted()
        DesktopKnowledgeGraphExtractionTasks(Path(previous.kb_dir)).recover_interrupted()


def _materialize_okf_projection_on_open(kb_dir: Path) -> None:
    """Preserve any valid on-disk projection; only repair a missing/invalid tree."""
    if not has_valid_okf_projection(kb_dir):
        materialize_okf_projection(kb_dir)
