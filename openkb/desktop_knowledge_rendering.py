"""Safe, domain-neutral rendering primitives for evidence-bound generated knowledge."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RenderedKnowledgeClaim:
    text: str
    source_markers: tuple[str, ...]
    applicability: tuple[tuple[str, str], ...] = ()


def render_generated_knowledge(
    kind: str,
    claims: tuple[RenderedKnowledgeClaim, ...],
    *,
    language: str | None = None,
) -> str:
    """Render claims without inventing a kind-, language-, or role-derived outline."""
    del kind, language
    return "\n\n".join(_claim_text(claim) for claim in claims)


def render_markdown_text(value: str) -> str:
    """Escape model/source text at the Markdown boundary without changing displayed text."""
    escaped = value.replace("\\", "\\\\")
    for marker in ("`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "+", "-", "!", "|"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


def _claim_text(claim: RenderedKnowledgeClaim) -> str:
    scope = "; ".join(
        f"{render_markdown_text(dimension)}: {render_markdown_text(value)}"
        for dimension, value in claim.applicability
    )
    applicability = f" ({scope})" if scope else ""
    markers = "".join(claim.source_markers)
    return f"{render_markdown_text(' '.join(claim.text.split()))}{applicability}{markers}"
