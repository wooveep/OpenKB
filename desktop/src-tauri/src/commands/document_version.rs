//! Native commands for the user-reviewed Document Version interface.

use super::run_engine;
use crate::engine::protocol::{
    BridgeError, DocumentLineageDecision, DocumentVersionCandidate,
    DocumentVersionCandidateDecision, DocumentVersionCandidatesResult,
    DocumentVersionCatalogSnapshot, DocumentVersionDiffsResult,
};
use crate::DesktopState;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_document_version_candidates(
    state: State<'_, DesktopState>,
) -> Result<DocumentVersionCandidatesResult, BridgeError> {
    run_engine(
        &state.engine,
        "document_version_candidates",
        move |engine| engine.document_version_candidates(),
    )
    .await
}

#[tauri::command]
pub(crate) async fn desktop_document_version_catalog(
    state: State<'_, DesktopState>,
) -> Result<DocumentVersionCatalogSnapshot, BridgeError> {
    run_engine(&state.engine, "document_version_catalog", move |engine| {
        engine.document_version_catalog()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_confirm_document_lineage(
    state: State<'_, DesktopState>,
    decision: DocumentLineageDecision,
    request_id: String,
) -> Result<DocumentVersionCatalogSnapshot, BridgeError> {
    run_engine(&state.engine, "confirm_document_lineage", move |engine| {
        engine.confirm_document_lineage(decision, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_document_version_diffs(
    state: State<'_, DesktopState>,
    lineage_id: String,
) -> Result<DocumentVersionDiffsResult, BridgeError> {
    run_engine(&state.engine, "document_version_diffs", move |engine| {
        engine.document_version_diffs(lineage_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_resolve_document_version_candidate(
    state: State<'_, DesktopState>,
    candidate_id: String,
    decision: DocumentVersionCandidateDecision,
    request_id: String,
) -> Result<DocumentVersionCandidate, BridgeError> {
    run_engine(
        &state.engine,
        "resolve_document_version_candidate",
        move |engine| engine.resolve_document_version_candidate(candidate_id, decision, request_id),
    )
    .await
}
