"""Focused behavior checks for current-KB command-palette search."""

import io

import pytest

from openkb.answers.conversations import DesktopConversationService
from openkb.engine.protocol import DesktopRequest, DesktopRequestError
from openkb.engine.server import DesktopEngineServer
from openkb.importing.service import DesktopTextImportService
from openkb.knowledge.pages.service import DesktopKnowledgePageService
from openkb.retrieval.global_search import search_desktop_knowledge_base
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


@pytest.mark.parametrize(
    "params",
    (
        {},
        {"query": None},
        {"query": False},
        {"query": 7},
        {"query": {"text": "provider"}},
        {"query": ["provider"]},
    ),
)
def test_engine_bridge_rejects_malformed_global_search_before_search(tmp_path, monkeypatch, params):
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(tmp_path / "desktop-kb")
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True
    monkeypatch.setattr(
        "openkb.engine.search.search_desktop_knowledge_base",
        lambda *_args: (_ for _ in ()).throw(AssertionError("search opened")),
    )

    with pytest.raises(DesktopRequestError) as captured:
        server._dispatch(
            DesktopRequest("global-search", "workbench.global_search", params),
            cancel_event=None,
        )

    assert captured.value.code == "invalid_params"


@pytest.mark.parametrize("query", ("", " \t\n"))
def test_engine_bridge_preserves_valid_empty_global_search(tmp_path, query):
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(tmp_path / "desktop-kb")
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    result = server._dispatch(
        DesktopRequest("global-search", "workbench.global_search", {"query": query}),
        cancel_event=None,
    )

    assert result == {"query": "", "results": []}


def test_global_search_returns_only_user_facing_workspace_content(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "network-guide.md"
    source.write_text("# Network guide\n\nCheck the provider endpoint.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Provider connectivity",
        content_markdown="# Use the configured endpoint",
    )
    pages.publish(page.page_id)
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


def test_global_search_excludes_working_draft_until_publication(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Release channel",
        content_markdown="# Stable marker cobalt",
    )
    pages.publish(draft.page_id)
    pages.save_draft(
        page_id=draft.page_id,
        kind="concept",
        title="Release channel",
        content_markdown="# Unpublished marker vermilion",
    )

    assert search_desktop_knowledge_base(kb_dir, "cobalt")["results"]
    assert search_desktop_knowledge_base(kb_dir, "vermilion")["results"] == []

    pages.publish(draft.page_id)

    assert search_desktop_knowledge_base(kb_dir, "vermilion")["results"]
