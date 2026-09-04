//! Native commands for the user-reviewed Document Version interface.

use crate::engine_protocol::{
    BridgeError, DocumentLineageDecision, DocumentVersionCandidate,
    DocumentVersionCandidateDecision, DocumentVersionCandidatesResult,
    DocumentVersionCatalogSnapshot, DocumentVersionDiffsResult,
};
use crate::DesktopState;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_document_version_candidates(
    state: State<'_, DesktopState>,
) -> Result<DocumentVersionCandidatesResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.document_version_candidates())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop document-version lookup stopped unexpectedly: {error}"),
        })?
}

#[tauri::command]
pub(crate) async fn desktop_document_version_catalog(
    state: State<'_, DesktopState>,
) -> Result<DocumentVersionCatalogSnapshot, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.document_version_catalog())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!(
                "Desktop Document Version Catalog lookup stopped unexpectedly: {error}"
            ),
        })?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_confirm_document_lineage(
    state: State<'_, DesktopState>,
    decision: DocumentLineageDecision,
    request_id: String,
) -> Result<DocumentVersionCatalogSnapshot, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.confirm_document_lineage(decision, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop document lineage confirmation stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_document_version_diffs(
    state: State<'_, DesktopState>,
    lineage_id: String,
) -> Result<DocumentVersionDiffsResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.document_version_diffs(lineage_id))
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop Document Version diff lookup stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_resolve_document_version_candidate(
    state: State<'_, DesktopState>,
    candidate_id: String,
    decision: DocumentVersionCandidateDecision,
    request_id: String,
) -> Result<DocumentVersionCandidate, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.resolve_document_version_candidate(candidate_id, decision, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop document-version decision stopped unexpectedly: {error}"),
    })?
}
