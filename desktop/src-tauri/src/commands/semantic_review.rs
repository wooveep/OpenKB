use super::run_engine;
use crate::engine::protocol::BridgeError;
use crate::engine::wire::semantic_reviews::{SemanticReviewDecision, SemanticReviews};
use crate::DesktopState;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_semantic_reviews(
    state: State<'_, DesktopState>,
) -> Result<SemanticReviews, BridgeError> {
    run_engine(&state.engine, "semantic_reviews", |engine| {
        engine.semantic_reviews()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_resolve_semantic_review(
    state: State<'_, DesktopState>,
    review_id: String,
    decision: SemanticReviewDecision,
    request_id: String,
) -> Result<SemanticReviews, BridgeError> {
    run_engine(&state.engine, "resolve_semantic_review", move |engine| {
        engine.resolve_semantic_review(review_id, decision, request_id)
    })
    .await
}
