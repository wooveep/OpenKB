"""Focused checks for SQLite-authoritative Desktop conversations."""

from __future__ import annotations

import sqlite3

from openkb.desktop_conversations import DesktopConversationService
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_conversation_persists_messages_versions_draft_and_selected_evidence(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_text(
        "# Guide\n\nOpenKB answers from current available evidence without embeddings.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    service = DesktopConversationService(kb_dir)

    created = service.create()
    conversation_id = str(created["conversation_id"])
    service.save_draft(conversation_id, "unsent question")
    answered = service.ask(conversation_id, "How does OpenKB answer?")
    assistant = answered["messages"][1]
    first_version = assistant["answer_versions"][0]

    assert answered["draft_text"] == ""
    assert [message["role"] for message in answered["messages"]] == ["user", "assistant"]
    assert first_version["citations"][0]["document_name"] == "guide.txt"
    assert assistant["selected_answer_version_id"] == first_version["answer_version_id"]

    regenerated = service.regenerate(conversation_id, assistant["message_id"])
    versions = regenerated["messages"][1]["answer_versions"]
    assert [version["version_number"] for version in versions] == [1, 2]
    service.select_answer_version(
        conversation_id,
        assistant["message_id"],
        first_version["answer_version_id"],
    )

    reopened = DesktopConversationService(kb_dir).get(conversation_id)
    assert (
        reopened["messages"][1]["selected_answer_version_id"]
        == first_version["answer_version_id"]
    )
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM grounded_answers").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_answer_versions"
        ).fetchone() == (2,)
