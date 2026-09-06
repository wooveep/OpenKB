//! Tauri commands for explicit Knowledge Reanalysis work.

use super::run_engine;
use crate::{
    engine::protocol::{BridgeError, KnowledgeReanalysisOverview, KnowledgeReanalysisRun},
    DesktopState,
};
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_knowledge_reanalysis(
    state: State<'_, DesktopState>,
) -> Result<KnowledgeReanalysisOverview, BridgeError> {
    run_engine(&state.engine, "knowledge_reanalysis", move |engine| {
        engine.knowledge_reanalysis()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_start_knowledge_reanalysis(
    state: State<'_, DesktopState>,
    document_ids: Vec<String>,
    request_id: String,
) -> Result<KnowledgeReanalysisRun, BridgeError> {
    run_engine(&state.engine, "start_knowledge_reanalysis", move |engine| {
        engine.start_knowledge_reanalysis(document_ids, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_retry_knowledge_reanalysis(
    state: State<'_, DesktopState>,
    job_id: String,
    request_id: String,
) -> Result<KnowledgeReanalysisRun, BridgeError> {
    run_engine(&state.engine, "retry_knowledge_reanalysis", move |engine| {
        engine.retry_knowledge_reanalysis(job_id, request_id)
    })
    .await
}
