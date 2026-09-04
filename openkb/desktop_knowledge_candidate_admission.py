"""Deterministic admission for model-proposed document knowledge candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from openkb.desktop_knowledge_entity_types import is_supported_entity_subtype

_FILE_OR_PACKAGE = re.compile(
    r"\.(?:deb|rpm|jar|dat|exe|dll|msi|zip|tar|gz|log|conf|ini|yaml|yml|json|xml|"
    r"sh|bat|cmd|ps1|sql|tmp|bak)(?:[?#].*)?$",
    re.IGNORECASE,
)
_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^[/\\]{1,2}|^[^\s/\\]+[/\\][^\s/\\]+)")
_URL_OR_ADDRESS = re.compile(
    r"(?:\b(?:https?|ftp)://|\b\d{1,3}(?:\.\d{1,3}){3}\b|"
    r"^[^\s@]+@[^\s@]+\.[^\s@]+$)",
    re.IGNORECASE,
)
_CONFIG_VALUE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\s*=\s*\S+$")
_COMMANDISH = re.compile(r"(?:^--?[a-z0-9_-]+$|\s--?[a-z0-9_-]+(?:\s|$)|[;&|]{2})", re.I)
_RELATION_PHRASE = re.compile(
    r"(?:\b(?:depends?\s+on|belongs?\s+to|is\s+(?:an?\s+)?part\s+of)\b|"
    r"(?:\u4f9d\u8d56\u4e8e|\u5c5e\u4e8e|\u662f.+(?:\u7ec4\u6210\u90e8\u5206|\u4e00\u90e8\u5206)))",
    re.IGNORECASE,
)
_OTHER_NAMED_ENTITY_REASON_CODES = frozenset(
    {"domain_specific_named_entity", "ontology_gap_named_entity"}
)
_SUPPORTED_KINDS = frozenset({"concept", "entity", "procedure"})
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
    decision_reasons: tuple[str, ...] = (),
) -> CandidateAdmission:
    """Reject obvious metadata/literals while leaving semantic judgment to analysis."""
    if kind not in _SUPPORTED_KINDS:
        return CandidateAdmission(False, "unsupported_kind")
    normalized_title = " ".join(title.split()).strip()
    folded = normalized_title.casefold()
    if not normalized_title:
        return CandidateAdmission(False, "empty_title")
    if len(normalized_title) > 160:
        return CandidateAdmission(False, "title_too_long")
    if folded in _NOISE_TITLES:
        return CandidateAdmission(False, "document_scaffolding")
    if _looks_like_raw_literal(normalized_title):
        return CandidateAdmission(False, "raw_literal")
    if kind == "entity" and _RELATION_PHRASE.search(normalized_title):
        return CandidateAdmission(False, "relation_phrase")
    substantive = tuple((role, text.strip()) for role, text in claims if len(text.strip()) >= 8)
    if not substantive:
        return CandidateAdmission(False, "no_substantive_claim")
    if kind == "entity":
        if not is_supported_entity_subtype(subtype):
            return CandidateAdmission(False, "unsupported_entity_subtype")
        mentioned = any(folded in text.casefold() for _role, text in substantive)
        if not mentioned:
            return CandidateAdmission(False, "entity_not_independently_described")
        if subtype == "other_named_entity" and not (
            _OTHER_NAMED_ENTITY_REASON_CODES.intersection(decision_reasons)
            and any(role in {"definition", "purpose"} for role, _text in substantive)
        ):
            return CandidateAdmission(False, "other_named_entity_requires_review_reason")
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


def _looks_like_raw_literal(title: str) -> bool:
    return any(
        pattern.search(title)
        for pattern in (
            _FILE_OR_PACKAGE,
            _PATH,
            _URL_OR_ADDRESS,
            _CONFIG_VALUE,
            _COMMANDISH,
        )
    )
