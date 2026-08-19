//! Tauri commands for SQLite-authoritative user Knowledge Pages.

use crate::engine_protocol::{BridgeError, KnowledgePage, KnowledgePageKind, KnowledgePagesResult};
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

fn join_error(operation: &'static str) -> impl FnOnce(tauri::Error) -> BridgeError {
    move |error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop knowledge-page {operation} stopped unexpectedly: {error}"),
    }
}
