//! Explicit PageTree enrichment controls for the private Engine transport.

use super::{BridgeError, BridgeResult, EngineSupervisor, PageTreeEnrichmentControlResult};
use serde_json::json;

impl EngineSupervisor {
    pub fn cancel_page_tree_enrichment(
        &self,
        document_id: String,
    ) -> BridgeResult<PageTreeEnrichmentControlResult> {
        self.page_tree_enrichment_control("workbench.cancel_page_tree_enrichment", document_id)
    }

    pub fn retry_page_tree_enrichment(
        &self,
        document_id: String,
    ) -> BridgeResult<PageTreeEnrichmentControlResult> {
        self.page_tree_enrichment_control("workbench.retry_page_tree_enrichment", document_id)
    }

    fn page_tree_enrichment_control(
        &self,
        method: &str,
        document_id: String,
    ) -> BridgeResult<PageTreeEnrichmentControlResult> {
        self.ensure_started()?;
        let value = self.request_started(method, json!({ "document_id": document_id }), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine PageTree enrichment control response is invalid: {error}"),
            )
        })
    }
}
