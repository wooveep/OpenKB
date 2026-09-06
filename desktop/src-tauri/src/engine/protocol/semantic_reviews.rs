use super::{BridgeError, BridgeResult, EngineSupervisor};
use crate::engine::wire::semantic_reviews::{SemanticReviewDecision, SemanticReviews};
use serde_json::json;

impl EngineSupervisor {
    pub fn semantic_reviews(&self) -> BridgeResult<SemanticReviews> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.knowledge_reconciliation_semantic_reviews",
            json!({}),
            None,
        )?;
        serde_json::from_value(value)
            .map_err(|error| BridgeError::new("invalid_engine_response", error.to_string()))
    }

    pub fn resolve_semantic_review(
        &self,
        review_id: String,
        decision: SemanticReviewDecision,
        request_id: String,
    ) -> BridgeResult<SemanticReviews> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.resolve_knowledge_reconciliation_semantic_review",
            json!({"review_id": review_id, "decision": decision}),
            Some(request_id),
        )?;
        serde_json::from_value(value)
            .map_err(|error| BridgeError::new("invalid_engine_response", error.to_string()))
    }
}
