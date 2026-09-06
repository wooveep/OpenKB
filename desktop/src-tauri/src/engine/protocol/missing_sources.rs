//! Missing Source Candidate requests over the private Engine protocol.

use super::{
    BridgeError, BridgeResult, EngineSupervisor, MissingSourceBindingResult,
    MissingSourceCandidatesResult, MissingSourceDismissalResult,
};
use serde_json::json;

impl EngineSupervisor {
    pub fn missing_source_candidates(&self) -> BridgeResult<MissingSourceCandidatesResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.knowledge_reconciliation_missing_sources",
            json!({}),
            None,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Missing Source queue has an invalid shape: {error}"),
            )
        })
    }

    pub fn bind_missing_source_candidate(
        &self,
        candidate_id: String,
        evidence_id: String,
        request_id: String,
    ) -> BridgeResult<MissingSourceBindingResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.bind_knowledge_reconciliation_missing_source",
            json!({"candidate_id": candidate_id, "evidence_id": evidence_id}),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Missing Source binding has an invalid shape: {error}"),
            )
        })
    }

    pub fn dismiss_missing_source_candidates(
        &self,
        candidate_ids: Vec<String>,
        request_id: String,
    ) -> BridgeResult<MissingSourceDismissalResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.dismiss_knowledge_reconciliation_missing_sources",
            json!({"candidate_ids": candidate_ids}),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Missing Source dismissal has an invalid shape: {error}"),
            )
        })
    }
}
