"""Focused checks for SQLite-authoritative Desktop conversations."""

from __future__ import annotations

import sqlite3

from openkb.desktop_conversations import DesktopConversationService
from openkb.desktop_document_version_catalog import (
    DocumentLineageDecision,
    DocumentVersionMemberDecision,
)
from openkb.desktop_document_versions import DesktopDocumentVersionService
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_version_scope import VersionFilter
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_conversation_persists_messages_versions_draft_and_selected_evidence(tmp_path, monkeypatch):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_text(
        "# Guide\n\nOpenKB answers from current available evidence without embeddings.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    service = DesktopConversationService(kb_dir)
    retry_flags: list[bool] = []
    generate = service._answers.generate

    def observed_generate(question: str, **kwargs):
        retry_flags.append(bool(kwargs.get("retry_suspended_operations", False)))
        return generate(question, **kwargs)

    monkeypatch.setattr(service._answers, "generate", observed_generate)

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
    assert retry_flags == [False, True]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            """
            UPDATE conversation_answer_citations
            SET channels_json = '["page_tree"]'
            WHERE answer_version_id = ?
            """,
            (first_version["answer_version_id"],),
        )
        connection.commit()
    service.select_answer_version(
        conversation_id,
        assistant["message_id"],
        first_version["answer_version_id"],
    )

    reopened = DesktopConversationService(kb_dir).get(conversation_id)
    assert (
        reopened["messages"][1]["selected_answer_version_id"] == first_version["answer_version_id"]
    )
    assert reopened["messages"][1]["answer_versions"][0]["citations"][0]["channels"] == [
        "structure_lexical"
    ]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM grounded_answers").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_answer_versions"
        ).fetchone() == (2,)


def test_conversation_inherits_the_pinned_lineage_and_accepts_an_exact_ui_filter(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    old_source = tmp_path / "Guide_V10.2.md"
    current_source = tmp_path / "Guide_V10.3.md"
    old_source.write_text("# Guide\n\nLegacy setting only.", encoding="utf-8")
    current_source.write_text("# Guide\n\nCurrent setting only.", encoding="utf-8")
    old = DesktopTextImportService(kb_dir).import_text(old_source).document
    current = DesktopTextImportService(kb_dir).import_text(current_source).document
    snapshot = DesktopDocumentVersionService(kb_dir).confirm_lineage(
        DocumentLineageDecision(
            display_name="Guide",
            version_scheme="numeric_dotted",
            members=(
                DocumentVersionMemberDecision(old.document_id, "V10.2"),
                DocumentVersionMemberDecision(
                    current.document_id,
                    "V10.3",
                    predecessor_document_id=old.document_id,
                ),
            ),
            current_document_id=current.document_id,
        )
    )
    lineage = next(item for item in snapshot.lineages if item.lineage_state == "confirmed")
    service = DesktopConversationService(kb_dir)
    conversation_id = str(service.create()["conversation_id"])

    exact = service.ask(
        conversation_id,
        "Which setting applies?",
        version_filter=VersionFilter(
            mode="exact",
            lineage_ids=(lineage.lineage_id,),
            document_ids=(current.document_id,),
            version_labels=("V10.3",),
        ),
    )
    follow_up = service.ask(conversation_id, "What setting does the previous version use?")

    exact_version = exact["messages"][1]["answer_versions"][0]
    follow_up_version = follow_up["messages"][3]["answer_versions"][0]
    assert {item["document_id"] for item in exact_version["citations"]} == {current.document_id}
    assert follow_up_version["retrieval_trace"]["version_scope_selection_reason"] == (
        "conversation_previous_version"
    )
    assert {item["document_id"] for item in follow_up_version["citations"]} == {old.document_id}
