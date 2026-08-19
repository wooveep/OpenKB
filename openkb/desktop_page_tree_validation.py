"""Query-time integrity checks for immutable current Document PageTrees."""

from __future__ import annotations

from openkb.desktop_page_tree import PageTreeGeneration


def validate_current_page_tree(generation: PageTreeGeneration) -> None:
    """Reject an incomplete or structurally inconsistent current generation."""
    nodes = generation.nodes
    if generation.status != "ready" or not nodes:
        raise ValueError("The current Document PageTree generation is invalid.")
    node_by_id = {node.node_id: node for node in nodes}
    if len(node_by_id) != len(nodes) or tuple(node.order for node in nodes) != tuple(
        range(len(nodes))
    ):
        raise ValueError("The current Document PageTree hierarchy is invalid.")
    roots = tuple(node for node in nodes if node.parent_node_id is None)
    if len(roots) != 1 or roots[0].order != 0 or roots[0].depth != 0:
        raise ValueError("The current Document PageTree root is invalid.")
    for node in nodes[1:]:
        parent = node_by_id.get(node.parent_node_id or "")
        if (
            parent is None
            or parent.order >= node.order
            or node.depth != parent.depth + 1
            or not node.kind
            or not node.title
        ):
            raise ValueError("The current Document PageTree hierarchy is invalid.")
