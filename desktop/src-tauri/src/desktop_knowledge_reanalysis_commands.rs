//! Tauri commands for explicit Knowledge Reanalysis work.

use crate::{
    engine_protocol::{BridgeError, KnowledgeReanalysisOverview, KnowledgeReanalysisRun},
    DesktopState,
};
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_knowledge_reanalysis(
    state: State<'_, DesktopState>,
) -> Result<KnowledgeReanalysisOverview, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.knowledge_reanalysis())
        .await
        .map_err(|error| BridgeError::new("desktop_command_failed", error.to_string()))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_start_knowledge_reanalysis(
    state: State<'_, DesktopState>,
    document_ids: Vec<String>,
    request_id: String,
) -> Result<KnowledgeReanalysisRun, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.start_knowledge_reanalysis(document_ids, request_id)
    })
    .await
    .map_err(|error| BridgeError::new("desktop_command_failed", error.to_string()))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_retry_knowledge_reanalysis(
    state: State<'_, DesktopState>,
    job_id: String,
    request_id: String,
) -> Result<KnowledgeReanalysisRun, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.retry_knowledge_reanalysis(job_id, request_id)
    })
    .await
    .map_err(|error| BridgeError::new("desktop_command_failed", error.to_string()))?
}
