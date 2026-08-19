"""Create/open Desktop Knowledge Bases with ordered runtime recovery."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb import desktop_engine_knowledge_reanalysis as reanalysis_engine
from openkb import desktop_knowledge_reanalysis as reanalysis_runtime
from openkb.desktop_conversations import recover_stale_conversation_generations
from openkb.desktop_okf_projection import materialize_okf_projection
from openkb.desktop_raw_assets import DesktopRawAssetService

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_knowledge_base_activation(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Change the active workspace only after invalidating old background work."""
    from openkb.desktop_engine import DesktopRequestError, _required_path_param

    kb_dir = Path(_required_path_param(request, "kb_dir"))
    name_value = request.params.get("name")
    if request.method == "workbench.create_knowledge_base" and (
        name_value is not None and not isinstance(name_value, str)
    ):
        raise DesktopRequestError(
            "invalid_params", "workbench.create_knowledge_base name must be a string."
        )
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
    materialize_okf_projection(active_kb_dir)
    server._start_recoverable_imports(active_kb_dir)
    return activation.as_dict()


def _interrupt_previous_reanalysis(server: DesktopEngineServer) -> None:
    previous = server._workspace.active()
    reanalysis_engine.invalidate_knowledge_reanalysis_workers(server)
    if previous is not None:
        reanalysis_runtime.recover_interrupted_knowledge_reanalysis(Path(previous.kb_dir))
