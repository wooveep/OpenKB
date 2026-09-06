"""Experimental official PageIndex adapter over OpenKB-owned Document IR."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from openkb.importing.artifacts import DocumentIRBlock, SourceImage
from openkb.page_tree.tree import (
    PageTreeEvidenceBinding,
    PageTreeGeneration,
    PageTreeImageBinding,
    PageTreeNode,
    build_deterministic_page_tree,
)
from openkb.page_tree.validation import validate_current_page_tree

PAGEINDEX_PACKAGE_VERSION = "0.2.10"
PAGEINDEX_SOURCE_COMMIT = "ba0ef02d78034704be049894c463dc606acbd0d7"
PAGEINDEX_PROVIDER_KIND = "official_pageindex"
PAGEINDEX_ADAPTER_VERSION = 1
PAGEINDEX_ADAPTER_SCHEMA = f"openkb.official-pageindex-adapter.v{PAGEINDEX_ADAPTER_VERSION}"
PAGEINDEX_PROVIDER_VERSION = (
    f"{PAGEINDEX_PACKAGE_VERSION}+{PAGEINDEX_SOURCE_COMMIT[:12]}.openkb{PAGEINDEX_ADAPTER_VERSION}"
)
PAGEINDEX_DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_PROVIDER_NODES = 4_096
_MAX_PROVIDER_DEPTH = 64
_MAX_TITLE_CHARS = 512
_MAX_SUMMARY_CHARS = 2_000

logger = logging.getLogger(__name__)
ProviderInvoker = Callable[[Path, Path, float], None]


class PageIndexProviderError(RuntimeError):
    """A stable, provider-local failure that must not disable baseline retrieval."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _RenderedBlock:
    ordinal: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class _ProviderNode:
    provider_id: str
    parent_index: int | None
    depth: int
    title: str
    summary: str | None
    start_line: int


def build_official_pageindex_generation(
    document_id: str,
    blocks: tuple[DocumentIRBlock, ...],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    images: tuple[SourceImage, ...],
    *,
    python_executable: Path | None = None,
    worker_executable: Path | None = None,
    timeout_seconds: float = PAGEINDEX_DEFAULT_TIMEOUT_SECONDS,
    invoke: ProviderInvoker | None = None,
) -> PageTreeGeneration:
    """Invoke and normalize PageIndex without reading or writing KB-local state."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Official PageIndex timeout must be positive.")
    shell = build_deterministic_page_tree(
        document_id,
        blocks,
        evidence,
        images,
        provider_kind=PAGEINDEX_PROVIDER_KIND,
        provider_version=PAGEINDEX_PROVIDER_VERSION,
    )
    markdown, rendered_blocks = _render_document_ir(blocks)
    provider_invoke = invoke or _subprocess_invoker(python_executable, worker_executable)
    try:
        with tempfile.TemporaryDirectory(prefix="openkb-pageindex-") as temporary:
            temporary_dir = Path(temporary)
            input_path = temporary_dir / "document.md"
            output_path = temporary_dir / "tree.json"
            input_path.write_text(markdown, encoding="utf-8")
            provider_invoke(input_path, output_path, timeout_seconds)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired as error:
        raise PageIndexProviderError(
            "pageindex_provider_timeout", "Official PageIndex tree generation timed out."
        ) from error
    except PageIndexProviderError:
        raise
    except (OSError, json.JSONDecodeError, RecursionError) as error:
        raise PageIndexProviderError(
            "pageindex_provider_unavailable", "Official PageIndex could not produce a tree."
        ) from error

    try:
        generation = _normalize_provider_tree(
            shell,
            blocks,
            evidence,
            images,
            rendered_blocks,
            payload,
            markdown.count("\n") + 1,
        )
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise PageIndexProviderError(
            "pageindex_provider_invalid_tree", "Official PageIndex returned an invalid tree."
        ) from error
    return generation


def validate_official_pageindex_generation(
    generation: PageTreeGeneration, expected: PageTreeGeneration
) -> None:
    """Validate normalized output against its current authoritative input shell."""
    validate_current_page_tree(generation)
    if not _same_generation_identity(generation, expected):
        raise ValueError("Official PageIndex generation identity changed.")
    if _evidence_identities(generation) != _evidence_identities(expected):
        raise ValueError("Official PageIndex Evidence bindings changed.")
    if _image_identities(generation) != _image_identities(expected):
        raise ValueError("Official PageIndex Source Image bindings changed.")


def _same_generation_identity(generation: PageTreeGeneration, expected: PageTreeGeneration) -> bool:
    return (
        generation.document_version_id == expected.document_version_id
        and generation.provider_kind == PAGEINDEX_PROVIDER_KIND
        and generation.provider_version == PAGEINDEX_PROVIDER_VERSION
        and generation.structural_ir_fingerprint == expected.structural_ir_fingerprint
        and generation.locator_mapping_digest == expected.locator_mapping_digest
        and generation.generation_id == _normalized_generation_id(expected, generation.nodes)
        and all(
            node.node_id == _node_id(generation.generation_id, "node", node.order)
            for node in generation.nodes
        )
    )


def _evidence_identities(generation: PageTreeGeneration) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted(
            (binding.evidence_id, binding.block_ordinal)
            for node in generation.nodes
            for binding in node.evidence
        )
    )


def _image_identities(generation: PageTreeGeneration) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted(
            (binding.source_image_id, binding.image_ordinal)
            for node in generation.nodes
            for binding in node.source_images
        )
    )


def _subprocess_invoker(
    python_executable: Path | None,
    worker_executable: Path | None = None,
) -> ProviderInvoker:
    if python_executable is not None and worker_executable is not None:
        raise ValueError("Select either an isolated Python runtime or a frozen PageIndex worker.")
    if python_executable is None and worker_executable is None:
        raise PageIndexProviderError(
            "pageindex_provider_not_configured",
            "An isolated official PageIndex runtime was not selected.",
        )
    # Do not resolve this path: POSIX virtual environments commonly expose
    # ``bin/python`` as a symlink, and resolving it drops the environment's
    # package context in the child process.
    selected = worker_executable if worker_executable is not None else python_executable
    assert selected is not None
    executable = Path(os.path.abspath(selected.expanduser()))
    source_worker = Path(__file__).with_name("worker.py")
    frozen = worker_executable is not None

    def invoke(input_path: Path, output_path: Path, timeout_seconds: float) -> None:
        environment = _provider_environment()
        try:
            command = (
                (str(executable), str(input_path), str(output_path))
                if frozen
                else (str(executable), str(source_worker), str(input_path), str(output_path))
            )
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=input_path.parent,
                env=environment,
            )
        except OSError as error:
            raise PageIndexProviderError(
                "pageindex_provider_not_configured",
                "The isolated official PageIndex runtime could not be started.",
            ) from error
        if completed.returncode != 0 or not output_path.is_file():
            logger.warning(
                "Official PageIndex worker failed return_code=%s stderr=%r",
                completed.returncode,
                completed.stderr[-800:],
            )
            raise PageIndexProviderError(
                "pageindex_provider_unavailable", "Official PageIndex tree generation failed."
            )

    return invoke


def _provider_environment() -> dict[str, str]:
    allowed = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _render_document_ir(
    blocks: tuple[DocumentIRBlock, ...],
) -> tuple[str, tuple[_RenderedBlock, ...]]:
    lines: list[str] = []
    rendered: list[_RenderedBlock] = []
    if not any(block.kind == "heading" for block in blocks):
        lines.extend(("# Document", ""))
    for block in blocks:
        start_line = len(lines) + 1
        if block.kind == "heading":
            level = min(6, max(1, len(block.heading_path)))
            title = " ".join(block.text.split()) or f"Section {block.ordinal + 1}"
            lines.append(f"{'#' * level} {title}")
        else:
            content_lines = block.text.splitlines() or [""]
            lines.extend(f"> {line}" for line in content_lines)
        rendered.append(_RenderedBlock(block.ordinal, start_line, len(lines)))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", tuple(rendered)


def _normalize_provider_tree(
    shell: PageTreeGeneration,
    blocks: tuple[DocumentIRBlock, ...],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    images: tuple[SourceImage, ...],
    rendered_blocks: tuple[_RenderedBlock, ...],
    payload: object,
    line_count: int,
) -> PageTreeGeneration:
    if not isinstance(payload, dict) or payload.get("line_count") != line_count:
        raise ValueError("Official PageIndex output does not match its Document IR input.")
    structure = payload.get("structure")
    if not isinstance(structure, list):
        raise ValueError("Official PageIndex output has no structure.")
    provider_nodes = _flatten_provider_nodes(structure)
    if not provider_nodes or len(provider_nodes) > _MAX_PROVIDER_NODES:
        raise ValueError("Official PageIndex output has an invalid node count.")
    starts = tuple(node.start_line for node in provider_nodes)
    if starts != tuple(sorted(starts)) or len({node.provider_id for node in provider_nodes}) != len(
        provider_nodes
    ):
        raise ValueError("Official PageIndex output order is invalid.")

    evidence_by_ordinal = {block.ordinal: evidence_id for evidence_id, block in evidence}
    blocks_by_ordinal = {block.ordinal: block for block in blocks}
    images_by_ordinal = _images_by_block_ordinal(shell)
    root_id = _node_id(shell.generation_id, "root", 0)
    nodes: list[PageTreeNode] = [PageTreeNode(root_id, None, 0, 0, "document", "Document", {})]
    normalized_ids: list[str] = []
    first_start = provider_nodes[0].start_line
    root_ordinals = tuple(item.ordinal for item in rendered_blocks if item.start_line < first_start)
    nodes[0] = _normalized_node(
        nodes[0], root_ordinals, evidence_by_ordinal, images_by_ordinal, blocks_by_ordinal
    )
    for index, provider_node in enumerate(provider_nodes):
        end_line = (
            provider_nodes[index + 1].start_line - 1
            if index + 1 < len(provider_nodes)
            else line_count
        )
        ordinals = tuple(
            item.ordinal
            for item in rendered_blocks
            if provider_node.start_line <= item.start_line <= end_line
        )
        node_id = _node_id(shell.generation_id, provider_node.provider_id, index + 1)
        normalized_ids.append(node_id)
        parent_id = (
            root_id
            if provider_node.parent_index is None
            else normalized_ids[provider_node.parent_index]
        )
        node = PageTreeNode(
            node_id=node_id,
            parent_node_id=parent_id,
            order=index + 1,
            depth=provider_node.depth + 1,
            kind="section",
            title=provider_node.title,
            summary=provider_node.summary,
            locator={
                "pageindex_line_start": provider_node.start_line,
                "pageindex_line_end": end_line,
            },
        )
        nodes.append(
            _normalized_node(
                node, ordinals, evidence_by_ordinal, images_by_ordinal, blocks_by_ordinal
            )
        )
    normalized_nodes = tuple(nodes)
    generation_id = _normalized_generation_id(shell, normalized_nodes)
    normalized_nodes = _rekey_nodes(generation_id, normalized_nodes)
    generation = PageTreeGeneration(
        generation_id=generation_id,
        document_version_id=shell.document_version_id,
        provider_kind=PAGEINDEX_PROVIDER_KIND,
        provider_version=PAGEINDEX_PROVIDER_VERSION,
        structural_ir_fingerprint=shell.structural_ir_fingerprint,
        locator_mapping_digest=shell.locator_mapping_digest,
        created_at=shell.created_at,
        status="ready",
        nodes=normalized_nodes,
    )
    validate_current_page_tree(generation)
    if {binding.evidence_id for node in normalized_nodes for binding in node.evidence} != set(
        evidence_by_ordinal.values()
    ):
        raise ValueError("Official PageIndex output did not preserve all Evidence references.")
    return generation


def _normalized_node(
    node: PageTreeNode,
    ordinals: tuple[int, ...],
    evidence_by_ordinal: Mapping[int, str],
    images_by_ordinal: Mapping[int, tuple[PageTreeImageBinding, ...]],
    blocks_by_ordinal: Mapping[int, DocumentIRBlock],
) -> PageTreeNode:
    locator = dict(node.locator)
    if ordinals:
        block = blocks_by_ordinal[ordinals[0]]
        locator["source_locator"] = dict(block.locator or {})
    image_bindings: list[PageTreeImageBinding] = []
    for ordinal in ordinals:
        for binding in images_by_ordinal.get(ordinal, ()):
            if binding not in image_bindings:
                image_bindings.append(binding)
    return PageTreeNode(
        node_id=node.node_id,
        parent_node_id=node.parent_node_id,
        order=node.order,
        depth=node.depth,
        kind=node.kind,
        title=node.title,
        summary=node.summary,
        locator=locator,
        evidence=tuple(
            PageTreeEvidenceBinding(evidence_by_ordinal[ordinal], ordinal) for ordinal in ordinals
        ),
        source_images=tuple(image_bindings),
    )


def _images_by_block_ordinal(
    shell: PageTreeGeneration,
) -> dict[int, tuple[PageTreeImageBinding, ...]]:
    values: dict[int, tuple[PageTreeImageBinding, ...]] = {}
    for node in shell.nodes:
        for evidence in node.evidence:
            values[evidence.block_ordinal] = node.source_images
    return values


def _flatten_provider_nodes(structure: list[object]) -> tuple[_ProviderNode, ...]:
    flattened: list[_ProviderNode] = []
    pending: list[tuple[object, int | None, int]] = [
        (value, None, 0) for value in reversed(structure)
    ]
    while pending:
        value, parent_index, depth = pending.pop()
        if not isinstance(value, dict):
            raise ValueError("Official PageIndex node is invalid.")
        provider_id = value.get("node_id")
        title = value.get("title")
        start_line = value.get("line_num")
        summary = value.get("summary") or value.get("prefix_summary")
        children = value.get("nodes", [])
        if not (
            isinstance(provider_id, str)
            and provider_id
            and isinstance(title, str)
            and 0 < len(title.strip()) <= _MAX_TITLE_CHARS
            and type(start_line) is int
            and start_line >= 1
            and (summary is None or isinstance(summary, str))
            and isinstance(children, list)
            and depth <= _MAX_PROVIDER_DEPTH
            and len(flattened) < _MAX_PROVIDER_NODES
        ):
            raise ValueError("Official PageIndex node fields are invalid.")
        index = len(flattened)
        flattened.append(
            _ProviderNode(
                provider_id,
                parent_index,
                depth,
                title.strip(),
                summary[:_MAX_SUMMARY_CHARS] if summary else None,
                start_line,
            )
        )
        pending.extend((child, index, depth + 1) for child in reversed(children))
    return tuple(flattened)


def _node_id(generation_id: str, provider_id: str, order: int) -> str:
    return hashlib.sha256(
        f"{PAGEINDEX_ADAPTER_SCHEMA}:{generation_id}:{provider_id}:{order}".encode()
    ).hexdigest()


def _normalized_generation_id(shell: PageTreeGeneration, nodes: tuple[PageTreeNode, ...]) -> str:
    order_by_id = {node.node_id: node.order for node in nodes}
    semantic_nodes: list[dict[str, object]] = []
    for node in sorted(nodes, key=lambda value: value.order):
        parent_order = None
        if node.parent_node_id is not None:
            parent_order = order_by_id.get(node.parent_node_id)
            if parent_order is None:
                raise ValueError("Official PageIndex node parent is invalid.")
        semantic_nodes.append(
            {
                "order": node.order,
                "parent_order": parent_order,
                "depth": node.depth,
                "kind": node.kind,
                "title": node.title,
                "summary": node.summary,
                "locator": node.locator,
                "evidence": [
                    [binding.evidence_id, binding.block_ordinal] for binding in node.evidence
                ],
                "source_images": [
                    [binding.source_image_id, binding.image_ordinal]
                    for binding in node.source_images
                ],
            }
        )
    return _digest(
        {
            "schema": PAGEINDEX_ADAPTER_SCHEMA,
            "input_generation_id": shell.generation_id,
            "nodes": semantic_nodes,
        }
    )


def _rekey_nodes(generation_id: str, nodes: tuple[PageTreeNode, ...]) -> tuple[PageTreeNode, ...]:
    order_by_id = {node.node_id: node.order for node in nodes}
    node_ids = {node.order: _node_id(generation_id, "node", node.order) for node in nodes}
    values: list[PageTreeNode] = []
    for node in nodes:
        parent_order = order_by_id[node.parent_node_id] if node.parent_node_id is not None else None
        values.append(
            PageTreeNode(
                node_id=node_ids[node.order],
                parent_node_id=node_ids[parent_order] if parent_order is not None else None,
                order=node.order,
                depth=node.depth,
                kind=node.kind,
                title=node.title,
                summary=node.summary,
                locator=dict(node.locator),
                evidence=node.evidence,
                source_images=node.source_images,
            )
        )
    return tuple(values)


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
