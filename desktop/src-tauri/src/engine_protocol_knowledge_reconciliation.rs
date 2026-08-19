//! Desktop requests for staged and committed knowledge reconciliation review.

use super::{
    BridgeError, BridgeResult, EngineSupervisor, KnowledgeReconciliationCommit,
    KnowledgeReconciliationConflictsResult, KnowledgeReconciliationDecision,
    IMPORT_REQUEST_TIMEOUT,
};
use serde_json::json;

impl EngineSupervisor {
    pub fn knowledge_reconciliation_conflicts(
        &self,
    ) -> BridgeResult<KnowledgeReconciliationConflictsResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.knowledge_reconciliation_conflicts",
            json!({}),
            None,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-reconciliation queue has an invalid shape: {error}"),
            )
        })
    }

    pub fn stage_knowledge_reconciliation_decisions(
        &self,
        candidate_ids: Vec<String>,
        decision: Option<KnowledgeReconciliationDecision>,
        manual_merge_content: Option<String>,
        request_id: String,
    ) -> BridgeResult<KnowledgeReconciliationConflictsResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.stage_knowledge_reconciliation_decisions",
            json!({
                "candidate_ids": candidate_ids,
                "decision": decision,
                "manual_merge_content": manual_merge_content,
            }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-reconciliation staging has an invalid shape: {error}"),
            )
        })
    }

    pub fn commit_knowledge_reconciliation_decisions(
        &self,
        request_id: String,
    ) -> BridgeResult<KnowledgeReconciliationCommit> {
        self.ensure_started()?;
        let value = self.request_started_with_timeout(
            "workbench.commit_knowledge_reconciliation_decisions",
            json!({}),
            Some(request_id),
            IMPORT_REQUEST_TIMEOUT,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-reconciliation commit has an invalid shape: {error}"),
            )
        })
    }
}
