//! Tauri commands for Missing Source review without leaking raw Engine values.

use crate::{
    engine_protocol::{
        BridgeError, MissingSourceBindingResult, MissingSourceCandidatesResult,
        MissingSourceDismissalResult,
    },
    DesktopState,
};
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_missing_source_candidates(
    state: State<'_, DesktopState>,
) -> Result<MissingSourceCandidatesResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.missing_source_candidates())
        .await
        .map_err(|error| BridgeError::new("desktop_command_failed", error.to_string()))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_bind_missing_source_candidate(
    state: State<'_, DesktopState>,
    candidate_id: String,
    evidence_id: String,
    request_id: String,
) -> Result<MissingSourceBindingResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.bind_missing_source_candidate(candidate_id, evidence_id, request_id)
    })
    .await
    .map_err(|error| BridgeError::new("desktop_command_failed", error.to_string()))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_dismiss_missing_source_candidates(
    state: State<'_, DesktopState>,
    candidate_ids: Vec<String>,
    request_id: String,
) -> Result<MissingSourceDismissalResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.dismiss_missing_source_candidates(candidate_ids, request_id)
    })
    .await
    .map_err(|error| BridgeError::new("desktop_command_failed", error.to_string()))?
}
