//! Tauri commands for SQLite-authoritative user Knowledge Pages.

use crate::engine_protocol::{
    BridgeError, KnowledgePage, KnowledgePageDeletionResult, KnowledgePageKind,
    KnowledgePagesResult, KnowledgeSourcesResult,
};
use crate::DesktopState;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_knowledge_pages(
    state: State<'_, DesktopState>,
) -> Result<KnowledgePagesResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.knowledge_pages())
        .await
        .map_err(join_error("lookup"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_get_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.knowledge_page(page_id))
        .await
        .map_err(join_error("read"))?
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
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.save_knowledge_page(page_id, kind, title, content_markdown, request_id)
    })
    .await
    .map_err(join_error("draft save"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_publish_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.publish_knowledge_page(page_id, request_id))
        .await
        .map_err(join_error("publication"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_verify_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.verify_knowledge_page(page_id, request_id))
        .await
        .map_err(join_error("verification"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_set_knowledge_page_stale_after(
    state: State<'_, DesktopState>,
    page_id: String,
    stale_after: Option<String>,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.set_knowledge_page_stale_after(page_id, stale_after, request_id)
    })
    .await
    .map_err(join_error("stale-after update"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_deprecate_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.deprecate_knowledge_page(page_id, request_id)
    })
    .await
    .map_err(join_error("deprecation"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_restore_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.restore_knowledge_page(page_id, request_id))
        .await
        .map_err(join_error("restore"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_permanently_delete_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
    confirmation_page_id: String,
    request_id: String,
) -> Result<KnowledgePageDeletionResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.permanently_delete_knowledge_page(page_id, confirmation_page_id, request_id)
    })
    .await
    .map_err(join_error("permanent deletion"))?
}

#[tauri::command]
pub(crate) async fn desktop_search_knowledge_sources(
    state: State<'_, DesktopState>,
    query: String,
) -> Result<KnowledgeSourcesResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.search_knowledge_sources(query))
        .await
        .map_err(join_error("source search"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_bind_knowledge_page_source(
    state: State<'_, DesktopState>,
    page_id: String,
    claim_text: String,
    evidence_id: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.bind_knowledge_page_source(page_id, claim_text, evidence_id, request_id)
    })
    .await
    .map_err(join_error("source binding"))?
}

fn join_error(operation: &'static str) -> impl FnOnce(tauri::Error) -> BridgeError {
    move |error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop knowledge-page {operation} stopped unexpectedly: {error}"),
    }
}
