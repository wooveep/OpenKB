//! Desktop D3 document-version review requests.

use super::{
    BridgeError, BridgeResult, DocumentLineageDecision, DocumentVersionCandidate,
    DocumentVersionCandidateDecision, DocumentVersionCandidatesResult,
    DocumentVersionCatalogSnapshot, DocumentVersionDiffsResult, EngineSupervisor,
};
use serde_json::json;

impl EngineSupervisor {
    pub fn document_version_catalog(&self) -> BridgeResult<DocumentVersionCatalogSnapshot> {
        self.ensure_started()?;
        let value = self.request_started("workbench.document_version_catalog", json!({}), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Document Version Catalog has an invalid shape: {error}"),
            )
        })
    }

    pub fn confirm_document_lineage(
        &self,
        decision: DocumentLineageDecision,
        request_id: String,
    ) -> BridgeResult<DocumentVersionCatalogSnapshot> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.confirm_document_lineage",
            json!({ "decision": decision }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine lineage confirmation has an invalid shape: {error}"),
            )
        })
    }

    pub fn document_version_diffs(
        &self,
        lineage_id: String,
    ) -> BridgeResult<DocumentVersionDiffsResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.document_version_diffs",
            json!({ "lineage_id": lineage_id }),
            None,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Document Version diffs have an invalid shape: {error}"),
            )
        })
    }

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
