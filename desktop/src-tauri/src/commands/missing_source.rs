//! Tauri commands for Missing Source review without leaking raw Engine values.

use super::run_engine;
use crate::{
    engine::protocol::{
        BridgeError, MissingSourceBindingResult, MissingSourceCandidatesResult,
        MissingSourceDismissalResult,
    },
    DesktopState,
};
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_missing_source_candidates(
    state: State<'_, DesktopState>,
) -> Result<MissingSourceCandidatesResult, BridgeError> {
    run_engine(&state.engine, "missing_source_candidates", move |engine| {
        engine.missing_source_candidates()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_bind_missing_source_candidate(
    state: State<'_, DesktopState>,
    candidate_id: String,
    evidence_id: String,
    request_id: String,
) -> Result<MissingSourceBindingResult, BridgeError> {
    run_engine(
        &state.engine,
        "bind_missing_source_candidate",
        move |engine| engine.bind_missing_source_candidate(candidate_id, evidence_id, request_id),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_dismiss_missing_source_candidates(
    state: State<'_, DesktopState>,
    candidate_ids: Vec<String>,
    request_id: String,
) -> Result<MissingSourceDismissalResult, BridgeError> {
    run_engine(
        &state.engine,
        "dismiss_missing_source_candidates",
        move |engine| engine.dismiss_missing_source_candidates(candidate_ids, request_id),
    )
    .await
}
