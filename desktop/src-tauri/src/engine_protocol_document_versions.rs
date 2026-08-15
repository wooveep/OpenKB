//! Desktop D3 document-version review requests.

use super::{
    BridgeError, BridgeResult, DocumentVersionCandidate, DocumentVersionCandidateDecision,
    DocumentVersionCandidatesResult, EngineSupervisor,
};
use serde_json::json;

impl EngineSupervisor {
    pub fn document_version_candidates(&self) -> BridgeResult<DocumentVersionCandidatesResult> {
        self.ensure_started()?;
        let value =
            self.request_started("workbench.document_version_candidates", json!({}), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine document-version candidate list has an invalid shape: {error}"),
            )
        })
    }

    pub fn resolve_document_version_candidate(
        &self,
        candidate_id: String,
        decision: DocumentVersionCandidateDecision,
        request_id: String,
    ) -> BridgeResult<DocumentVersionCandidate> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.resolve_document_version_candidate",
            json!({ "candidate_id": candidate_id, "decision": decision }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine document-version decision has an invalid shape: {error}"),
            )
        })
    }
}
