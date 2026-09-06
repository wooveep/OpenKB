"""One read surface over generated snapshots and user-owned knowledge pages."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.knowledge.pages.generations import current_generation_id_in
from openkb.knowledge.pages.metadata import decode_knowledge_labels
from openkb.knowledge.pages.service import (
    DesktopKnowledgePageService,
    knowledge_page_summaries_in,
)
from openkb.knowledge.pages.sources import generation_source_map_in
from openkb.locks import kb_read_lock
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

KnowledgeAuthority = Literal["generated", "user"]


@dataclass(frozen=True)
class DesktopKnowledgeWorkspaceItem:
    authority: KnowledgeAuthority
    identity: str
    kind: str
    title: str
    updated_at: str
    current: bool
    generation_id: int | None = None
    item_key: str | None = None
    page_id: str | None = None
    publication_state: str | None = None
    provenance_state: str | None = None
    lifecycle_state: str | None = None

    def as_dict(self) -> dict[str, object]:
        common: dict[str, object] = {
            "authority": self.authority,
            "identity": self.identity,
            "kind": self.kind,
            "title": self.title,
            "updated_at": self.updated_at,
            "current": self.current,
        }
        if self.authority == "generated":
            if self.generation_id is None or self.item_key is None or self.provenance_state is None:
                raise ValueError("knowledge_workspace_generated_summary_invalid")
            return {
                **common,
                "generation_id": self.generation_id,
                "item_key": self.item_key,
                "provenance_state": self.provenance_state,
            }
        if self.page_id is None or self.publication_state is None or self.lifecycle_state is None:
            raise ValueError("knowledge_workspace_user_summary_invalid")
        return {
            **common,
            "page_id": self.page_id,
            "publication_state": self.publication_state,
            "lifecycle_state": self.lifecycle_state,
        }


@dataclass(frozen=True)
class DesktopKnowledgeWorkspace:
    current_generation_id: int | None
    items: tuple[DesktopKnowledgeWorkspaceItem, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "current_generation_id": self.current_generation_id,
            "items": [item.as_dict() for item in self.items],
        }


class DesktopKnowledgeWorkspaceService:
    """Compose separate knowledge authorities without copying or rewriting either."""

    def __init__(self, kb_dir: Path) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)

    def list_items(self, *, query: str = "") -> DesktopKnowledgeWorkspace:
        normalized = _normalized_query(query)
        with kb_read_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                generation_id = current_generation_id_in(connection)
                items = [
                    *_generated_summaries_in(connection, generation_id, normalized, current=True),
                    *(
                        DesktopKnowledgeWorkspaceItem(
                            authority="user",
                            identity=f"user:{page.page_id}",
                            kind=page.kind,
                            title=page.title,
                            updated_at=page.updated_at,
                            current=True,
                            page_id=page.page_id,
                            publication_state=page.publication_state,
                            lifecycle_state=page.lifecycle_state,
                        )
                        for page in knowledge_page_summaries_in(connection, query=normalized)
                    ),
                ]
                connection.rollback()
            finally:
                connection.close()
        return DesktopKnowledgeWorkspace(
            generation_id,
            tuple(
                sorted(
                    items,
                    key=lambda item: (
                        item.kind.casefold(),
                        item.title.casefold(),
                        item.authority,
                        item.identity,
                    ),
                )
            ),
        )

    def history(self, *, generation_id: int | None = None) -> dict[str, object]:
        """List immutable generation snapshots or inspect one explicitly selected snapshot."""
        with kb_read_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                current_generation_id = current_generation_id_in(connection)
                if generation_id is None:
                    rows = connection.execute(
                        """
                        SELECT generations.generation_id, generations.parent_generation_id,
                            generations.created_at, COUNT(items.item_key)
                        FROM knowledge_generations AS generations
                        LEFT JOIN knowledge_generation_items AS items
                            ON items.generation_id = generations.generation_id
                        GROUP BY generations.generation_id
                        ORDER BY generations.generation_id DESC
                        """
                    ).fetchall()
                    result: dict[str, object] = {
                        "current_generation_id": current_generation_id,
                        "generations": [
                            {
                                "generation_id": int(row[0]),
                                "parent_generation_id": (
                                    int(row[1]) if row[1] is not None else None
                                ),
                                "created_at": str(row[2]),
                                "item_count": int(row[3]),
                                "current": int(row[0]) == current_generation_id,
                            }
                            for row in rows
                        ],
                    }
                else:
                    row = connection.execute(
                        """
                        SELECT parent_generation_id, created_at
                        FROM knowledge_generations WHERE generation_id = ?
                        """,
                        (generation_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError("knowledge_generation_not_found")
                    is_current = generation_id == current_generation_id
                    result = {
                        "generation_id": generation_id,
                        "parent_generation_id": int(row[0]) if row[0] is not None else None,
                        "created_at": str(row[1]),
                        "current": is_current,
                        "items": [
                            item.as_dict()
                            for item in _generated_summaries_in(
                                connection, generation_id, "", current=is_current
                            )
                        ],
                    }
                connection.rollback()
                return result
            finally:
                connection.close()

    def generated_item(self, generation_id: int, item_key: str) -> dict[str, object]:
        with kb_read_lock(self.state_dir):
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT items.kind, items.title, items.content_markdown,
                        items.aliases_json, items.identity_labels_json,
                        items.created_at, items.provenance_state,
                        items.analysis_provenance_json,
                        state.current_generation_id = items.generation_id
                    FROM knowledge_generation_items AS items
                    LEFT JOIN knowledge_generation_state AS state ON state.singleton = 1
                    WHERE items.generation_id = ? AND items.item_key = ?
                    """,
                    (generation_id, item_key),
                ).fetchone()
                if row is None:
                    raise ValueError("knowledge_workspace_item_not_found")
                sources = generation_source_map_in(connection, generation_id, item_key)
            finally:
                connection.close()
        return {
            "authority": "generated",
            "identity": f"generated:{generation_id}:{item_key}",
            "generation_id": generation_id,
            "item_key": item_key,
            "kind": str(row[0]),
            "title": str(row[1]),
            "content_markdown": str(row[2]),
            "aliases": list(decode_knowledge_labels(row[3])),
            "identity_labels": list(decode_knowledge_labels(row[4])),
            "created_at": str(row[5]),
            "provenance_state": str(row[6]),
            "analysis_provenance": _json_object(row[7]),
            "source_map": [source.as_dict() for source in sources],
            "current": bool(row[8]),
            "editable": False,
        }

    def user_item(self, page_id: str) -> dict[str, object]:
        return {
            "authority": "user",
            "identity": f"user:{page_id}",
            "editable": True,
            **DesktopKnowledgePageService(self.kb_dir).get_page(page_id).as_dict(),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = connect_database(self.database_path)
        return connection


def _generated_summaries_in(
    connection: sqlite3.Connection,
    generation_id: int | None,
    query: str,
    *,
    current: bool,
) -> tuple[DesktopKnowledgeWorkspaceItem, ...]:
    if generation_id is None:
        return ()
    rows = connection.execute(
        """
        SELECT item_key, kind, title, created_at, provenance_state
        FROM knowledge_generation_items
        WHERE generation_id = ?
            AND (
                ? = ''
                OR instr(lower(title), ?) > 0
                OR instr(lower(content_markdown), ?) > 0
                OR instr(lower(aliases_json), ?) > 0
                OR instr(lower(identity_labels_json), ?) > 0
            )
        ORDER BY kind, title COLLATE NOCASE, item_key
        """,
        (generation_id, query, query, query, query, query),
    ).fetchall()
    return tuple(
        DesktopKnowledgeWorkspaceItem(
            authority="generated",
            identity=f"generated:{generation_id}:{row[0]}",
            kind=str(row[1]),
            title=str(row[2]),
            updated_at=str(row[3]),
            current=current,
            generation_id=generation_id,
            item_key=str(row[0]),
            provenance_state=str(row[4]),
        )
        for row in rows
    )


def _normalized_query(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("knowledge_workspace_query_invalid")
    return " ".join(value.split()).casefold()[:500]


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
