//! Desktop Concept/Entity page requests owned by the Python SQLite authority.

use super::{
    BridgeError, BridgeResult, EngineSupervisor, KnowledgeAdoptionDecision,
    KnowledgeAdoptionResult, KnowledgeExportMode, KnowledgeExportPreview, KnowledgeExportResult,
    KnowledgePage, KnowledgePageDeletionResult, KnowledgePageKind, KnowledgePagesResult,
    KnowledgeSourcesResult, KnowledgeWorkspaceHistory, KnowledgeWorkspaceItemDetail,
    KnowledgeWorkspaceItemRequest, KnowledgeWorkspaceResult, LONG_REQUEST_TIMEOUT,
};
use serde_json::json;

impl EngineSupervisor {
    pub fn knowledge_workspace(&self, query: String) -> BridgeResult<KnowledgeWorkspaceResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.knowledge_workspace",
            json!({ "query": query }),
            None,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge Workspace response has an invalid shape: {error}"),
            )
        })
    }

    pub fn knowledge_workspace_item(
        &self,
        item: KnowledgeWorkspaceItemRequest,
    ) -> BridgeResult<KnowledgeWorkspaceItemDetail> {
        self.ensure_started()?;
        let params = match item {
            KnowledgeWorkspaceItemRequest::Generated(item) => json!({
                "authority": "generated",
                "generation_id": item.generation_id,
                "item_key": item.item_key,
            }),
            KnowledgeWorkspaceItemRequest::User(item) => json!({
                "authority": "user",
                "page_id": item.page_id,
            }),
        };
        let value = self.request_started("workbench.knowledge_workspace_item", params, None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge Workspace item has an invalid shape: {error}"),
            )
        })
    }

    pub fn knowledge_workspace_history(
        &self,
        generation_id: Option<u64>,
    ) -> BridgeResult<KnowledgeWorkspaceHistory> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.knowledge_workspace_history",
            json!({ "generation_id": generation_id }),
            None,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge Workspace history has an invalid shape: {error}"),
            )
        })
    }

    pub fn adopt_knowledge_item(
        &self,
        generation_id: u64,
        item_key: String,
        adoption_request_id: String,
        request_id: String,
        decision: Option<KnowledgeAdoptionDecision>,
        candidate_page_id: Option<String>,
    ) -> BridgeResult<KnowledgeAdoptionResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.adopt_knowledge_item",
            json!({
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": adoption_request_id,
                "adoption_decision": decision,
                "candidate_page_id": candidate_page_id,
            }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge Adoption response has an invalid shape: {error}"),
            )
        })
    }

    pub fn knowledge_pages(&self) -> BridgeResult<KnowledgePagesResult> {
        self.ensure_started()?;
        let value = self.request_started("workbench.knowledge_pages", json!({}), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-page list has an invalid shape: {error}"),
            )
        })
    }

    pub fn knowledge_page(&self, page_id: String) -> BridgeResult<KnowledgePage> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.knowledge_page",
            json!({ "page_id": page_id }),
            None,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-page response has an invalid shape: {error}"),
            )
        })
    }

    pub fn save_knowledge_page(
        &self,
        page_id: Option<String>,
        kind: KnowledgePageKind,
        title: String,
        content_markdown: String,
        request_id: String,
    ) -> BridgeResult<KnowledgePage> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.save_knowledge_page",
            json!({
                "page_id": page_id,
                "kind": kind,
                "title": title,
                "content_markdown": content_markdown,
            }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-page save response has an invalid shape: {error}"),
            )
        })
    }

    pub fn publish_knowledge_page(
        &self,
        page_id: String,
        request_id: String,
    ) -> BridgeResult<KnowledgePage> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.publish_knowledge_page",
            json!({ "page_id": page_id }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-page publish response has an invalid shape: {error}"),
            )
        })
    }

    pub fn verify_knowledge_page(
        &self,
        page_id: String,
        request_id: String,
    ) -> BridgeResult<KnowledgePage> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.verify_knowledge_page",
            json!({ "page_id": page_id }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge verification response has an invalid shape: {error}"),
            )
        })
    }

    pub fn set_knowledge_page_stale_after(
        &self,
        page_id: String,
        stale_after: Option<String>,
        request_id: String,
    ) -> BridgeResult<KnowledgePage> {
        self.knowledge_page_mutation(
            "workbench.set_knowledge_page_stale_after",
            json!({ "page_id": page_id, "stale_after": stale_after }),
            request_id,
            "stale-after",
        )
    }

    pub fn deprecate_knowledge_page(
        &self,
        page_id: String,
        request_id: String,
    ) -> BridgeResult<KnowledgePage> {
        self.knowledge_page_mutation(
            "workbench.deprecate_knowledge_page",
            json!({ "page_id": page_id }),
            request_id,
            "deprecation",
        )
    }

    pub fn restore_knowledge_page(
        &self,
        page_id: String,
        request_id: String,
    ) -> BridgeResult<KnowledgePage> {
        self.knowledge_page_mutation(
            "workbench.restore_knowledge_page",
            json!({ "page_id": page_id }),
            request_id,
            "restore",
        )
    }

    pub fn permanently_delete_knowledge_page(
        &self,
        page_id: String,
        confirmation_page_id: String,
        request_id: String,
    ) -> BridgeResult<KnowledgePageDeletionResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.permanently_delete_knowledge_page",
            json!({
                "page_id": page_id,
                "confirmation_page_id": confirmation_page_id,
            }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-page deletion response has an invalid shape: {error}"),
            )
        })
    }

    pub fn search_knowledge_sources(&self, query: String) -> BridgeResult<KnowledgeSourcesResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.search_knowledge_sources",
            json!({ "query": query }),
            None,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-source search has an invalid shape: {error}"),
            )
        })
    }

    pub fn bind_knowledge_page_source(
        &self,
        page_id: String,
        claim_text: String,
        evidence_id: String,
        request_id: String,
    ) -> BridgeResult<KnowledgePage> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.bind_knowledge_page_source",
            json!({
                "page_id": page_id,
                "claim_text": claim_text,
                "evidence_id": evidence_id,
            }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-source binding has an invalid shape: {error}"),
            )
        })
    }

    pub fn export_knowledge_bundle(
        &self,
        destination: String,
        mode: KnowledgeExportMode,
        request_id: String,
        expected_snapshot_id: Option<String>,
    ) -> BridgeResult<KnowledgeExportResult> {
        self.ensure_started()?;
        let value = self.request_started_with_wait(
            "workbench.export_knowledge_bundle",
            json!({
                "destination": destination,
                "mode": mode,
                "expected_snapshot_id": expected_snapshot_id,
            }),
            Some(request_id),
            Some(LONG_REQUEST_TIMEOUT),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge export response has an invalid shape: {error}"),
            )
        })
    }

    pub fn preview_knowledge_bundle(
        &self,
        mode: KnowledgeExportMode,
    ) -> BridgeResult<KnowledgeExportPreview> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.preview_knowledge_bundle",
            json!({ "mode": mode }),
            None,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine Knowledge export preview has an invalid shape: {error}"),
            )
        })
    }

    fn knowledge_page_mutation(
        &self,
        method: &str,
        params: serde_json::Value,
        request_id: String,
        operation: &str,
    ) -> BridgeResult<KnowledgePage> {
        self.ensure_started()?;
        let value = self.request_started(method, params, Some(request_id))?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-page {operation} response has an invalid shape: {error}"),
            )
        })
    }
}
