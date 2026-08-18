"""Focused behavior checks for current-KB command-palette search."""

from openkb.desktop_conversations import DesktopConversationService
from openkb.desktop_global_search import search_desktop_knowledge_base
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_global_search_returns_only_user_facing_workspace_content(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "network-guide.md"
    source.write_text("# Network guide\n\nCheck the provider endpoint.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    page = DesktopKnowledgePageService(kb_dir).save_page(
        page_id=None,
        kind="concept",
        title="Provider connectivity",
        content_markdown="Use the configured endpoint.",
    )
    conversation = DesktopConversationService(kb_dir).create("Network diagnosis")

    document_results = search_desktop_knowledge_base(kb_dir, "provider")["results"]
    conversation_results = search_desktop_knowledge_base(kb_dir, "diagnosis")["results"]

    assert {result["result_id"] for result in document_results} == {
        f"document:{imported.document.document_id}",
        f"knowledge_page:{page.page_id}",
    }
    assert [result["result_id"] for result in conversation_results] == [
        f"conversation:{conversation['conversation_id']}"
    ]
