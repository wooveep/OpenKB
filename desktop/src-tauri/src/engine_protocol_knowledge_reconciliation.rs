//! Desktop requests for the read-only knowledge reconciliation queue.

use super::{BridgeError, BridgeResult, EngineSupervisor, KnowledgeReconciliationConflictsResult};
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
}
