"""Deterministic admission for model-proposed document knowledge candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass

_RAW_LITERAL = re.compile(
    r"(?:^[A-Za-z]:[\\/]|^[/\\]{1,2}|https?://|\b\d{1,3}(?:\.\d{1,3}){3}\b|"
    r"\.(?:exe|dll|msi|zip|tar|gz|log|conf|ini|yaml|yml|json|xml|sh|bat|cmd|ps1)$)",
    re.IGNORECASE,
)
_COMMANDISH = re.compile(r"(?:^--?[a-z0-9_-]+$|\s--?[a-z0-9_-]+(?:\s|$)|[;&|]{2})", re.I)
_NOISE_TITLES = frozenset(
    {
        "目录",
        "修订记录",
        "版本记录",
        "注意事项",
        "前言",
        "附录",
        "table of contents",
        "revision history",
        "change log",
        "changelog",
        "notes",
        "appendix",
    }
)
_COMPLETION_TERMS = (
    "验证",
    "确认",
    "完成",
    "成功",
    "检查",
    "validate",
    "verify",
    "confirm",
    "complete",
    "success",
    "check",
)


@dataclass(frozen=True)
class CandidateAdmission:
    admitted: bool
    reason: str


def assess_knowledge_candidate(
    *,
    kind: str,
    title: str,
    subtype: str | None,
    claims: tuple[tuple[str, str], ...],
) -> CandidateAdmission:
    """Reject obvious metadata/literals while leaving semantic judgment to analysis."""
    normalized_title = " ".join(title.split()).strip()
    folded = normalized_title.casefold()
    if not normalized_title:
        return CandidateAdmission(False, "empty_title")
    if len(normalized_title) > 160:
        return CandidateAdmission(False, "title_too_long")
    if folded in _NOISE_TITLES:
        return CandidateAdmission(False, "document_scaffolding")
    if _RAW_LITERAL.search(normalized_title) or _COMMANDISH.search(normalized_title):
        return CandidateAdmission(False, "raw_literal")
    substantive = tuple((role, text.strip()) for role, text in claims if len(text.strip()) >= 8)
    if not substantive:
        return CandidateAdmission(False, "no_substantive_claim")
    if kind == "entity":
        mentioned = any(folded in text.casefold() for _role, text in substantive)
        if subtype is None and not mentioned:
            return CandidateAdmission(False, "entity_not_independently_described")
    if kind == "procedure":
        roles = {role for role, _text in substantive}
        if "step" not in roles:
            return CandidateAdmission(False, "procedure_without_step")
        has_completion = "validation" in roles or any(
            term in text.casefold() for _role, text in substantive for term in _COMPLETION_TERMS
        )
        if not has_completion:
            return CandidateAdmission(False, "procedure_without_completion")
    return CandidateAdmission(True, "admitted")
