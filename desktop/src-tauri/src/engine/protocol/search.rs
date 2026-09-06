//! Current-KB global search request for the Desktop command palette.

use super::{validated_response, BridgeResult, EngineSupervisor};
use serde::Deserialize;
use serde_json::{json, Value};

#[allow(dead_code)]
#[derive(Deserialize)]
struct SearchResponse {
    query: String,
    results: Vec<SearchResult>,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct SearchResult {
    result_id: String,
    kind: SearchKind,
    title: String,
    snippet: String,
    status: SearchStatus,
    document_id: Option<String>,
    page_id: Option<String>,
    conversation_id: Option<String>,
    message_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum SearchKind {
    Document,
    KnowledgePage,
    Conversation,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum SearchStatus {
    Available,
    Failed,
}

impl EngineSupervisor {
    pub fn global_search(&self, query: String) -> BridgeResult<Value> {
        self.ensure_started()?;
        let value =
            self.request_started("workbench.global_search", json!({ "query": query }), None)?;
        validated_response::<SearchResponse>(value, "global search")
    }
}
