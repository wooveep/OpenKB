"""Deterministic Document PageTree provider and import checkpoint contract."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock, SourceImage

if TYPE_CHECKING:
    from openkb.desktop_import_store import DesktopImportStore, ImportJobState

PAGE_TREE_CHECKPOINT_SCHEMA = "openkb.document-page-tree.v1"
DETERMINISTIC_PROVIDER_KIND = "openkb_deterministic"
DETERMINISTIC_PROVIDER_VERSION = "2"
PAGE_TREE_STAGE = "deterministic_page_tree"
PAGE_TREE_FAILURE_CODE = "deterministic_page_tree_failed"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PageTreeEvidenceBinding:
    evidence_id: str
    block_ordinal: int


@dataclass(frozen=True)
class PageTreeImageBinding:
    source_image_id: str
    image_ordinal: int


@dataclass(frozen=True)
class PageTreeNode:
    node_id: str
    parent_node_id: str | None
    order: int
    depth: int
    kind: str
    title: str
    locator: dict[str, object]
    evidence: tuple[PageTreeEvidenceBinding, ...] = ()
    source_images: tuple[PageTreeImageBinding, ...] = ()
    summary: str | None = None


@dataclass(frozen=True)
class PageTreeGeneration:
    generation_id: str
    document_version_id: str
    provider_kind: str
    provider_version: str
    structural_ir_fingerprint: str
    locator_mapping_digest: str
    created_at: str
    status: str
    nodes: tuple[PageTreeNode, ...]
    reused_from_generation_id: str | None = None


@dataclass(frozen=True)
class PageTreeStageOutcome:
    document_version_id: str
    generation: PageTreeGeneration | None
    error_code: str | None = None


def build_deterministic_page_tree(
    document_version_id: str,
    blocks: tuple[DocumentIRBlock, ...],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    source_images: tuple[SourceImage, ...] = (),
    *,
    created_at: str | None = None,
    provider_kind: str = DETERMINISTIC_PROVIDER_KIND,
    provider_version: str = DETERMINISTIC_PROVIDER_VERSION,
) -> PageTreeGeneration:
    """Build one immutable hierarchy from validated IR without calling a model."""
    if (
        not document_version_id
        or not provider_kind
        or not provider_version
        or not blocks
        or len(evidence) != len(blocks)
    ):
        raise ValueError("Document PageTree input is incomplete.")
    block_by_id = {block.block_id: block for block in blocks}
    evidence_by_block = {block.block_id: evidence_id for evidence_id, block in evidence}
    if (
        len(block_by_id) != len(blocks)
        or len(evidence_by_block) != len(blocks)
        or set(evidence_by_block) != set(block_by_id)
        or any(block.ordinal != ordinal for ordinal, block in enumerate(blocks))
    ):
        raise ValueError("Document PageTree IR and Evidence bindings are invalid.")
    images_by_id = {image.image_id: image for image in source_images}
    if len(images_by_id) != len(source_images):
        raise ValueError("Document PageTree Source Image bindings are invalid.")

    structural_fingerprint = _structural_fingerprint(blocks)
    locator_digest = _locator_mapping_digest(blocks, source_images)
    generation_id = _digest(
        {
            "schema": PAGE_TREE_CHECKPOINT_SCHEMA,
            "document_version_id": document_version_id,
            "provider_kind": provider_kind,
            "provider_version": provider_version,
            "structural_ir_fingerprint": structural_fingerprint,
            "locator_mapping_digest": locator_digest,
        }
    )
    root_id = _node_id(generation_id, 0, "document")
    nodes: list[PageTreeNode] = [PageTreeNode(root_id, None, 0, 0, "document", "Document", {})]
    depths = {root_id: 0}
    section_nodes: dict[tuple[str, ...], str] = {}
    for block in blocks:
        order = len(nodes)
        path = tuple(block.heading_path)
        if block.kind == "heading":
            parent_id = _nearest_section(section_nodes, path[:-1], root_id)
            kind = "section"
        else:
            parent_id = _nearest_section(section_nodes, path, root_id)
            kind = block.kind
        node_id = _node_id(generation_id, order, kind)
        image_bindings = _image_bindings(block, images_by_id)
        node = PageTreeNode(
            node_id=node_id,
            parent_node_id=parent_id,
            order=order,
            depth=depths[parent_id] + 1,
            kind=kind,
            title=_node_title(block, kind),
            locator=_block_locator(block),
            evidence=(PageTreeEvidenceBinding(evidence_by_block[block.block_id], block.ordinal),),
            source_images=image_bindings,
        )
        nodes.append(node)
        depths[node_id] = node.depth
        if block.kind == "heading":
            section_nodes[path] = node_id
    return PageTreeGeneration(
        generation_id=generation_id,
        document_version_id=document_version_id,
        provider_kind=provider_kind,
        provider_version=provider_version,
        structural_ir_fingerprint=structural_fingerprint,
        locator_mapping_digest=locator_digest,
        created_at=created_at or _timestamp(),
        status="ready",
        nodes=tuple(nodes),
    )


def prepare_import_page_tree(
    *,
    store: DesktopImportStore,
    state: ImportJobState,
    stage_run_id: str,
    stage_status: str,
    blocks: tuple[DocumentIRBlock, ...],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    source_images: tuple[SourceImage, ...],
    honor_control: Callable[[], None],
) -> PageTreeStageOutcome:
    """Checkpoint a best-effort tree while preserving one Document Version identity."""
    if stage_status in {"completed", "skipped"}:
        checkpoint = store.checkpoint(stage_run_id)
        if checkpoint is None:
            raise _checkpoint_error("Document PageTree checkpoint is missing.")
        try:
            return page_tree_outcome_from_checkpoint(checkpoint)
        except ValueError as error:
            raise _checkpoint_error(str(error)) from error

    document_version_id = uuid.uuid4().hex
    honor_control()
    store.set_stage(state, PAGE_TREE_STAGE, "running", 77)
    try:
        generation = build_deterministic_page_tree(
            document_version_id, blocks, evidence, source_images
        )
    except Exception:
        logger.exception("Deterministic Document PageTree build failed for %s", state.job_id)
        outcome = PageTreeStageOutcome(document_version_id, None, PAGE_TREE_FAILURE_CODE)
        store.set_stage(
            state,
            PAGE_TREE_STAGE,
            "skipped",
            79,
            checkpoint=page_tree_checkpoint(outcome),
            error_code=PAGE_TREE_FAILURE_CODE,
        )
        return outcome
    outcome = PageTreeStageOutcome(document_version_id, generation)
    store.set_stage(
        state,
        PAGE_TREE_STAGE,
        "completed",
        79,
        checkpoint=page_tree_checkpoint(outcome),
    )
    return outcome


def page_tree_checkpoint(outcome: PageTreeStageOutcome) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": PAGE_TREE_CHECKPOINT_SCHEMA,
        "status": "completed" if outcome.generation is not None else "failed",
        "document_version_id": outcome.document_version_id,
        "error_code": outcome.error_code,
    }
    if outcome.generation is not None:
        payload["generation"] = _generation_payload(outcome.generation)
    return payload


def page_tree_outcome_from_checkpoint(payload: object) -> PageTreeStageOutcome:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != PAGE_TREE_CHECKPOINT_SCHEMA
    ):
        raise ValueError("Document PageTree checkpoint is invalid.")
    document_id = payload.get("document_version_id")
    status = payload.get("status")
    error_code = payload.get("error_code")
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("Document PageTree checkpoint is invalid.")
    if status == "failed" and isinstance(error_code, str) and error_code:
        return PageTreeStageOutcome(document_id, None, error_code)
    if status != "completed" or error_code is not None:
        raise ValueError("Document PageTree checkpoint is invalid.")
    generation = _generation_from_payload(payload.get("generation"))
    if generation.document_version_id != document_id:
        raise ValueError("Document PageTree checkpoint identity is invalid.")
    return PageTreeStageOutcome(document_id, generation)


def page_tree_analysis_sections(
    generation: PageTreeGeneration,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> tuple[tuple[tuple[str, DocumentIRBlock], ...], ...]:
    """Project ordered Evidence into the tree's natural section boundaries."""
    nodes = {node.node_id: node for node in generation.nodes}
    evidence_owner: dict[str, str] = {}
    for node in generation.nodes:
        owner = node.node_id if node.kind == "section" else _section_ancestor(node, nodes)
        for binding in node.evidence:
            if binding.evidence_id in evidence_owner:
                return ()
            evidence_owner[binding.evidence_id] = owner
    if set(evidence_owner) != {evidence_id for evidence_id, _block in evidence}:
        return ()
    grouped: OrderedDict[str, list[tuple[str, DocumentIRBlock]]] = OrderedDict()
    for item in evidence:
        grouped.setdefault(evidence_owner[item[0]], []).append(item)
    return tuple(tuple(items) for items in grouped.values() if items)


def _generation_payload(generation: PageTreeGeneration) -> dict[str, object]:
    return {
        "generation_id": generation.generation_id,
        "document_version_id": generation.document_version_id,
        "provider_kind": generation.provider_kind,
        "provider_version": generation.provider_version,
        "structural_ir_fingerprint": generation.structural_ir_fingerprint,
        "locator_mapping_digest": generation.locator_mapping_digest,
        "created_at": generation.created_at,
        "status": generation.status,
        "reused_from_generation_id": generation.reused_from_generation_id,
        "nodes": [
            {
                "node_id": node.node_id,
                "parent_node_id": node.parent_node_id,
                "order": node.order,
                "depth": node.depth,
                "kind": node.kind,
                "title": node.title,
                "summary": node.summary,
                "locator": node.locator,
                "evidence_ids": [binding.evidence_id for binding in node.evidence],
                "evidence_block_ordinals": [binding.block_ordinal for binding in node.evidence],
                "source_image_ids": [binding.source_image_id for binding in node.source_images],
                "source_image_ordinals": [binding.image_ordinal for binding in node.source_images],
            }
            for node in generation.nodes
        ],
    }


def _generation_from_payload(payload: object) -> PageTreeGeneration:
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise ValueError("Document PageTree generation checkpoint is invalid.")
    required = (
        "generation_id",
        "document_version_id",
        "provider_kind",
        "provider_version",
        "structural_ir_fingerprint",
        "locator_mapping_digest",
        "created_at",
        "status",
    )
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
        raise ValueError("Document PageTree generation checkpoint is invalid.")
    reused_from = payload.get("reused_from_generation_id")
    if reused_from is not None and (not isinstance(reused_from, str) or not reused_from):
        raise ValueError("Document PageTree generation checkpoint is invalid.")
    nodes = tuple(_node_from_payload(value) for value in payload["nodes"])
    if not nodes or any(node.order != order for order, node in enumerate(nodes)):
        raise ValueError("Document PageTree generation node order is invalid.")
    node_ids = {node.node_id for node in nodes}
    if len(node_ids) != len(nodes) or any(
        node.parent_node_id is not None and node.parent_node_id not in node_ids for node in nodes
    ):
        raise ValueError("Document PageTree hierarchy is invalid.")
    return PageTreeGeneration(
        generation_id=str(payload["generation_id"]),
        document_version_id=str(payload["document_version_id"]),
        provider_kind=str(payload["provider_kind"]),
        provider_version=str(payload["provider_version"]),
        structural_ir_fingerprint=str(payload["structural_ir_fingerprint"]),
        locator_mapping_digest=str(payload["locator_mapping_digest"]),
        created_at=str(payload["created_at"]),
        status=str(payload["status"]),
        nodes=nodes,
        reused_from_generation_id=str(reused_from) if reused_from is not None else None,
    )


def _node_from_payload(payload: object) -> PageTreeNode:
    if not isinstance(payload, dict):
        raise ValueError("Document PageTree node checkpoint is invalid.")
    evidence_ids = payload.get("evidence_ids")
    evidence_ordinals = payload.get("evidence_block_ordinals")
    image_ids = payload.get("source_image_ids")
    image_ordinals = payload.get("source_image_ordinals")
    if not (
        isinstance(payload.get("node_id"), str)
        and payload["node_id"]
        and (payload.get("parent_node_id") is None or isinstance(payload["parent_node_id"], str))
        and type(payload.get("order")) is int
        and type(payload.get("depth")) is int
        and isinstance(payload.get("kind"), str)
        and payload["kind"]
        and isinstance(payload.get("title"), str)
        and isinstance(payload.get("locator"), dict)
        and isinstance(evidence_ids, list)
        and isinstance(evidence_ordinals, list)
        and len(evidence_ids) == len(evidence_ordinals)
        and all(isinstance(value, str) and value for value in evidence_ids)
        and all(type(value) is int and value >= 0 for value in evidence_ordinals)
        and isinstance(image_ids, list)
        and isinstance(image_ordinals, list)
        and len(image_ids) == len(image_ordinals)
        and all(isinstance(value, str) and value for value in image_ids)
        and all(type(value) is int and value >= 0 for value in image_ordinals)
    ):
        raise ValueError("Document PageTree node checkpoint is invalid.")
    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("Document PageTree node checkpoint is invalid.")
    return PageTreeNode(
        node_id=str(payload["node_id"]),
        parent_node_id=(
            str(payload["parent_node_id"]) if payload.get("parent_node_id") is not None else None
        ),
        order=int(payload["order"]),
        depth=int(payload["depth"]),
        kind=str(payload["kind"]),
        title=str(payload["title"]),
        summary=summary,
        locator=dict(payload["locator"]),
        evidence=tuple(
            PageTreeEvidenceBinding(str(evidence_id), int(ordinal))
            for evidence_id, ordinal in zip(evidence_ids, evidence_ordinals, strict=True)
        ),
        source_images=tuple(
            PageTreeImageBinding(str(image_id), int(ordinal))
            for image_id, ordinal in zip(image_ids, image_ordinals, strict=True)
        ),
    )


def _structural_fingerprint(blocks: tuple[DocumentIRBlock, ...]) -> str:
    return _digest(
        {
            "schema": "openkb.document-page-tree.structure.v1",
            "blocks": [
                {
                    "ordinal": block.ordinal,
                    "kind": block.kind,
                    "heading_path": list(block.heading_path),
                    "text_sha256": hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
                }
                for block in blocks
            ],
        }
    )


def _locator_mapping_digest(
    blocks: tuple[DocumentIRBlock, ...], source_images: tuple[SourceImage, ...]
) -> str:
    images_by_id = {image.image_id: image for image in source_images}
    return _digest(
        {
            "schema": "openkb.document-page-tree.locators.v1",
            "blocks": [
                {
                    "ordinal": block.ordinal,
                    "locator": _stable_block_locator(block, images_by_id),
                }
                for block in blocks
            ],
            "source_images": [
                {
                    "ordinal": image.ordinal,
                    "image_sha256": image.image_sha256,
                    "locator": image.locator,
                }
                for image in source_images
            ],
        }
    )


def _stable_block_locator(
    block: DocumentIRBlock, images_by_id: Mapping[str, SourceImage]
) -> dict[str, object]:
    locator = _block_locator(block)
    image_id = locator.get("source_image_id")
    if image_id is not None:
        locator["source_image_id"] = _stable_image_reference(image_id, images_by_id)
    image_ids = locator.get("source_image_ids")
    if image_ids is not None:
        if not isinstance(image_ids, list):
            raise ValueError("Document PageTree Source Image locator is invalid.")
        locator["source_image_ids"] = [
            _stable_image_reference(value, images_by_id) for value in image_ids
        ]
    return locator


def _stable_image_reference(
    value: object, images_by_id: Mapping[str, SourceImage]
) -> dict[str, object]:
    if not isinstance(value, str) or value not in images_by_id:
        raise ValueError("Document PageTree block references an unknown Source Image.")
    image = images_by_id[value]
    return {"ordinal": image.ordinal, "image_sha256": image.image_sha256}


def _image_bindings(
    block: DocumentIRBlock, images_by_id: Mapping[str, SourceImage]
) -> tuple[PageTreeImageBinding, ...]:
    locator = block.locator or {}
    values: list[object] = []
    if "source_image_id" in locator:
        values.append(locator["source_image_id"])
    source_image_ids = locator.get("source_image_ids")
    if isinstance(source_image_ids, list):
        values.extend(source_image_ids)
    bindings: list[PageTreeImageBinding] = []
    for value in values:
        if not isinstance(value, str) or value not in images_by_id:
            raise ValueError("Document PageTree block references an unknown Source Image.")
        image = images_by_id[value]
        binding = PageTreeImageBinding(image.image_id, image.ordinal)
        if binding not in bindings:
            bindings.append(binding)
    return tuple(bindings)


def _nearest_section(
    sections: Mapping[tuple[str, ...], str], path: tuple[str, ...], root_id: str
) -> str:
    for length in range(len(path), 0, -1):
        node_id = sections.get(path[:length])
        if node_id is not None:
            return node_id
    return root_id


def _section_ancestor(node: PageTreeNode, nodes: Mapping[str, PageTreeNode]) -> str:
    parent_id = node.parent_node_id
    while parent_id is not None:
        parent = nodes[parent_id]
        if parent.kind == "section":
            return parent.node_id
        parent_id = parent.parent_node_id
    return next(value.node_id for value in nodes.values() if value.parent_node_id is None)


def _node_title(block: DocumentIRBlock, kind: str) -> str:
    if kind == "section":
        text = " ".join(block.text.split())
        return text if len(text) <= 160 else f"{text[:157].rstrip()}..."
    label = f"{kind.replace('_', ' ').title()} {block.ordinal + 1}"
    return f"{block.heading_path[-1]} · {label}" if block.heading_path else label


def _block_locator(block: DocumentIRBlock) -> dict[str, object]:
    return (
        dict(block.locator)
        if block.locator is not None
        else {"line_start": block.line_start, "line_end": block.line_end}
    )


def _node_id(generation_id: str, order: int, kind: str) -> str:
    return hashlib.sha256(f"{generation_id}:{order}:{kind}".encode("utf-8")).hexdigest()


def _digest(payload: object) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _checkpoint_error(message: str) -> DesktopImportError:
    return DesktopImportError("import_checkpoint_invalid", message)
