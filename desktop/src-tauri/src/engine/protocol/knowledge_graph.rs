//! Explicit Knowledge Graph extraction controls for the private Engine transport.

use super::{BridgeError, BridgeResult, EngineSupervisor, KnowledgeGraphExtractionControlResult};
use serde_json::json;

impl EngineSupervisor {
    pub fn cancel_knowledge_graph_extraction(
        &self,
        document_id: String,
    ) -> BridgeResult<KnowledgeGraphExtractionControlResult> {
        self.knowledge_graph_control("workbench.cancel_knowledge_graph_extraction", document_id)
    }

    pub fn retry_knowledge_graph_extraction(
        &self,
        document_id: String,
    ) -> BridgeResult<KnowledgeGraphExtractionControlResult> {
        self.knowledge_graph_control("workbench.retry_knowledge_graph_extraction", document_id)
    }

    fn knowledge_graph_control(
        &self,
        method: &str,
        document_id: String,
    ) -> BridgeResult<KnowledgeGraphExtractionControlResult> {
        self.ensure_started()?;
        let value = self.request_started(method, json!({ "document_id": document_id }), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge Graph control response is invalid: {error}"),
            )
        })
    }
}
