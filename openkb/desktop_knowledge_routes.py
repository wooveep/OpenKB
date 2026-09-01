"""Stable semantic routes shared by virtual navigation and portable exports."""

from __future__ import annotations

import re
from pathlib import PurePath

_DIRECTORIES = {
    "concept": "concepts",
    "entity": "entities",
    "procedure": "procedures",
}
_SLUG_UNSAFE = re.compile(r"[^\w\u3400-\u9fff-]+", re.UNICODE)


def semantic_slug(title: str, fallback: str) -> str:
    slug = _SLUG_UNSAFE.sub("-", title.casefold()).strip("-_")
    return slug[:96] or fallback[:24]


def knowledge_route(kind: str, authority: str, title: str, identity: str) -> str:
    slug = semantic_slug(title, identity)
    if authority == "published_generation":
        return f"generated/{kind}/{slug}"
    return f"{_DIRECTORIES[kind]}/{slug}"


def summary_route(title: str, document_id: str) -> str:
    return f"summaries/{semantic_slug(_document_title(title), document_id)}"


def source_route(title: str, document_id: str) -> str:
    return f"sources/{semantic_slug(_document_title(title), document_id)}"


def _document_title(title: str) -> str:
    stem = PurePath(title).stem
    return stem if stem and stem != "." else title
