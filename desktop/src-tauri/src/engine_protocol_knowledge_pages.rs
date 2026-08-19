//! Desktop Concept/Entity page requests owned by the Python SQLite authority.

use super::{
    BridgeError, BridgeResult, EngineSupervisor, KnowledgePage, KnowledgePageDeletionResult,
    KnowledgePageKind, KnowledgePagesResult, KnowledgeSourcesResult,
};
use serde_json::json;

impl EngineSupervisor {
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
