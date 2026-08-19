//! Knowledge Reanalysis requests over the private Engine protocol.

use super::{
    BridgeError, BridgeResult, EngineSupervisor, KnowledgeReanalysisOverview,
    KnowledgeReanalysisRun,
};
use serde_json::json;

impl EngineSupervisor {
    pub fn knowledge_reanalysis(&self) -> BridgeResult<KnowledgeReanalysisOverview> {
        self.ensure_started()?;
        let value = self.request_started("workbench.knowledge_reanalysis", json!({}), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge Reanalysis overview has an invalid shape: {error}"),
            )
        })
    }

    pub fn start_knowledge_reanalysis(
        &self,
        document_ids: Vec<String>,
        request_id: String,
    ) -> BridgeResult<KnowledgeReanalysisRun> {
        self.knowledge_reanalysis_mutation(
            "workbench.start_knowledge_reanalysis",
            json!({"document_ids": document_ids}),
            request_id,
        )
    }

    pub fn retry_knowledge_reanalysis(
        &self,
        job_id: String,
        request_id: String,
    ) -> BridgeResult<KnowledgeReanalysisRun> {
        self.knowledge_reanalysis_mutation(
            "workbench.retry_knowledge_reanalysis",
            json!({"job_id": job_id}),
            request_id,
        )
    }

    fn knowledge_reanalysis_mutation(
        &self,
        method: &str,
        params: serde_json::Value,
        request_id: String,
    ) -> BridgeResult<KnowledgeReanalysisRun> {
        self.ensure_started()?;
        let value = self.request_started(method, params, Some(request_id))?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge Reanalysis run has an invalid shape: {error}"),
            )
        })
    }
}
