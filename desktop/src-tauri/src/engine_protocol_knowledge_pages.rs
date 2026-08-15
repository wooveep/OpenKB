//! Desktop Concept/Entity page requests owned by the Python SQLite authority.

use super::{
    BridgeError, BridgeResult, EngineSupervisor, KnowledgePage, KnowledgePageKind,
    KnowledgePagesResult,
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
}
