//! Tauri commands for SQLite-authoritative user Knowledge Pages.

use super::run_engine;
use crate::engine::protocol::{
    BridgeError, KnowledgeAdoptionDecision, KnowledgeAdoptionResult, KnowledgeExportMode,
    KnowledgeExportPreview, KnowledgeExportResult, KnowledgePage, KnowledgePageDeletionResult,
    KnowledgePageKind, KnowledgePagesResult, KnowledgeSourcesResult, KnowledgeWorkspaceHistory,
    KnowledgeWorkspaceItemDetail, KnowledgeWorkspaceItemRequest, KnowledgeWorkspaceResult,
};
use crate::DesktopState;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_knowledge_workspace(
    state: State<'_, DesktopState>,
    query: String,
) -> Result<KnowledgeWorkspaceResult, BridgeError> {
    run_engine(&state.engine, "knowledge_workspace", move |engine| {
        engine.knowledge_workspace(query)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_get_knowledge_workspace_item(
    state: State<'_, DesktopState>,
    item: KnowledgeWorkspaceItemRequest,
) -> Result<KnowledgeWorkspaceItemDetail, BridgeError> {
    run_engine(
        &state.engine,
        "get_knowledge_workspace_item",
        move |engine| engine.knowledge_workspace_item(item),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_knowledge_workspace_history(
    state: State<'_, DesktopState>,
    generation_id: Option<u64>,
) -> Result<KnowledgeWorkspaceHistory, BridgeError> {
    run_engine(
        &state.engine,
        "knowledge_workspace_history",
        move |engine| engine.knowledge_workspace_history(generation_id),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_adopt_knowledge_item(
    state: State<'_, DesktopState>,
    generation_id: u64,
    item_key: String,
    adoption_request_id: String,
    request_id: String,
    decision: Option<KnowledgeAdoptionDecision>,
    candidate_page_id: Option<String>,
) -> Result<KnowledgeAdoptionResult, BridgeError> {
    run_engine(&state.engine, "adopt_knowledge_item", move |engine| {
        engine.adopt_knowledge_item(
            generation_id,
            item_key,
            adoption_request_id,
            request_id,
            decision,
            candidate_page_id,
        )
    })
    .await
}

#[tauri::command]
pub(crate) async fn desktop_knowledge_pages(
    state: State<'_, DesktopState>,
) -> Result<KnowledgePagesResult, BridgeError> {
    run_engine(&state.engine, "knowledge_pages", move |engine| {
        engine.knowledge_pages()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_get_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
) -> Result<KnowledgePage, BridgeError> {
    run_engine(&state.engine, "get_knowledge_page", move |engine| {
        engine.knowledge_page(page_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_save_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: Option<String>,
    kind: KnowledgePageKind,
    title: String,
    content_markdown: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    run_engine(&state.engine, "save_knowledge_page", move |engine| {
        engine.save_knowledge_page(page_id, kind, title, content_markdown, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_publish_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    run_engine(&state.engine, "publish_knowledge_page", move |engine| {
        engine.publish_knowledge_page(page_id, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_verify_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    run_engine(&state.engine, "verify_knowledge_page", move |engine| {
        engine.verify_knowledge_page(page_id, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_set_knowledge_page_stale_after(
    state: State<'_, DesktopState>,
    page_id: String,
    stale_after: Option<String>,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    run_engine(
        &state.engine,
        "set_knowledge_page_stale_after",
        move |engine| engine.set_knowledge_page_stale_after(page_id, stale_after, request_id),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_deprecate_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    run_engine(&state.engine, "deprecate_knowledge_page", move |engine| {
        engine.deprecate_knowledge_page(page_id, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_restore_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    run_engine(&state.engine, "restore_knowledge_page", move |engine| {
        engine.restore_knowledge_page(page_id, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_permanently_delete_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    confirmation_page_id: String,
    request_id: String,
) -> Result<KnowledgePageDeletionResult, BridgeError> {
    run_engine(
        &state.engine,
        "permanently_delete_knowledge_page",
        move |engine| {
            engine.permanently_delete_knowledge_page(page_id, confirmation_page_id, request_id)
        },
    )
    .await
}

#[tauri::command]
pub(crate) async fn desktop_search_knowledge_sources(
    state: State<'_, DesktopState>,
    query: String,
) -> Result<KnowledgeSourcesResult, BridgeError> {
    run_engine(&state.engine, "search_knowledge_sources", move |engine| {
        engine.search_knowledge_sources(query)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_bind_knowledge_page_source(
    state: State<'_, DesktopState>,
    page_id: String,
    claim_text: String,
    evidence_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    run_engine(&state.engine, "bind_knowledge_page_source", move |engine| {
        engine.bind_knowledge_page_source(page_id, claim_text, evidence_id, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_export_knowledge_bundle(
    state: State<'_, DesktopState>,
    destination: String,
    mode: KnowledgeExportMode,
    request_id: String,
    expected_snapshot_id: Option<String>,
) -> Result<KnowledgeExportResult, BridgeError> {
    run_engine(&state.engine, "export_knowledge_bundle", move |engine| {
        engine.export_knowledge_bundle(destination, mode, request_id, expected_snapshot_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_preview_knowledge_bundle(
    state: State<'_, DesktopState>,
    mode: KnowledgeExportMode,
) -> Result<KnowledgeExportPreview, BridgeError> {
    run_engine(&state.engine, "preview_knowledge_bundle", move |engine| {
        engine.preview_knowledge_bundle(mode)
    })
    .await
}
