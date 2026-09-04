//! Conversation request methods for the private Engine transport.

use super::{validated_response, BridgeResult, EngineSupervisor, VersionFilter};
use crate::engine_wire::RetrievalTrace;
use serde::Deserialize;
use serde_json::{json, Value};

#[allow(dead_code)]
#[derive(Deserialize)]
struct ConversationList {
    conversations: Vec<ConversationSummary>,
    last_conversation_id: Option<String>,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct ConversationSummary {
    conversation_id: String,
    title: String,
    draft_text: String,
    created_at: String,
    updated_at: String,
    generating: bool,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct Conversation {
    conversation_id: String,
    title: String,
    draft_text: String,
    created_at: String,
    updated_at: String,
    messages: Vec<ConversationMessage>,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct ConversationMessage {
    message_id: String,
    ordinal: u64,
    role: MessageRole,
    content: String,
    status: MessageStatus,
    selected_answer_version_id: Option<String>,
    created_at: String,
    updated_at: String,
    answer_versions: Vec<AnswerVersion>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum MessageRole {
    User,
    Assistant,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum MessageStatus {
    Completed,
    Generating,
    Interrupted,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct AnswerVersion {
    answer_version_id: String,
    version_number: u64,
    answer_text: String,
    retrieval_plan: RetrievalPlan,
    citations: Vec<EvidenceRef>,
    source_images: Vec<SourceImage>,
    #[serde(default)]
    retrieval_trace: RetrievalTrace,
    degradations: Vec<String>,
    status: AnswerStatus,
    interruption_code: Option<String>,
    interruption_reason: Option<String>,
    created_at: String,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct RetrievalPlan {
    query: String,
    terms: Vec<String>,
    source: String,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct EvidenceRef {
    evidence_id: String,
    document_id: String,
    document_name: String,
    section: String,
    locator: Value,
    excerpt: String,
    channels: Vec<String>,
    #[serde(default)]
    version_label: Option<String>,
    #[serde(default)]
    version_side: Option<String>,
    source_available: bool,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct SourceImage {
    source_image_id: String,
    evidence_id: String,
    document_id: String,
    document_name: String,
    name: String,
    media_type: String,
    file_path: String,
    alt_text: Option<String>,
    locator: Value,
    source_available: bool,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum AnswerStatus {
    Completed,
    Interrupted,
}

fn conversation(value: Value) -> BridgeResult<Value> {
    validated_response::<Conversation>(value, "conversation")
}

fn conversation_list(value: Value) -> BridgeResult<Value> {
    validated_response::<ConversationList>(value, "conversation list")
}

impl EngineSupervisor {
    pub fn conversations(&self, search: String) -> BridgeResult<Value> {
        self.ensure_started()?;
        let value =
            self.request_started("workbench.conversations", json!({ "search": search }), None)?;
        conversation_list(value)
    }

    pub fn conversation(&self, conversation_id: String) -> BridgeResult<Value> {
        self.ensure_started()?;
        conversation(self.request_started(
            "workbench.conversation",
            json!({ "conversation_id": conversation_id }),
            None,
        )?)
    }

    pub fn create_conversation(
        &self,
        title: Option<String>,
        request_id: String,
    ) -> BridgeResult<Value> {
        self.ensure_started()?;
        conversation(self.request_started(
            "workbench.create_conversation",
            json!({ "title": title }),
            Some(request_id),
        )?)
    }

    pub fn rename_conversation(
        &self,
        conversation_id: String,
        title: String,
        request_id: String,
    ) -> BridgeResult<Value> {
        self.ensure_started()?;
        conversation(self.request_started(
            "workbench.rename_conversation",
            json!({ "conversation_id": conversation_id, "title": title }),
            Some(request_id),
        )?)
    }

    pub fn delete_conversation(
        &self,
        conversation_id: String,
        request_id: String,
    ) -> BridgeResult<Value> {
        self.ensure_started()?;
        conversation_list(self.request_started(
            "workbench.delete_conversation",
            json!({ "conversation_id": conversation_id }),
            Some(request_id),
        )?)
    }

    pub fn save_conversation_draft(
        &self,
        conversation_id: String,
        draft_text: String,
        request_id: String,
    ) -> BridgeResult<Value> {
        self.ensure_started()?;
        conversation(self.request_started(
            "workbench.save_conversation_draft",
            json!({ "conversation_id": conversation_id, "draft_text": draft_text }),
            Some(request_id),
        )?)
    }

    pub fn ask_conversation(
        &self,
        conversation_id: String,
        question: String,
        version_filter: Option<VersionFilter>,
        request_id: String,
    ) -> BridgeResult<Value> {
        self.ensure_started()?;
        conversation(self.request_started_with_wait(
            "workbench.ask_conversation",
            json!({
                "conversation_id": conversation_id,
                "question": question,
                "version_filter": version_filter,
            }),
            Some(request_id),
            None,
        )?)
    }

    pub fn regenerate_conversation_answer(
        &self,
        conversation_id: String,
        assistant_message_id: String,
        request_id: String,
    ) -> BridgeResult<Value> {
        self.ensure_started()?;
        conversation(self.request_started_with_wait(
            "workbench.regenerate_conversation_answer",
            json!({
                "conversation_id": conversation_id,
                "assistant_message_id": assistant_message_id,
            }),
            Some(request_id),
            None,
        )?)
    }

    pub fn select_answer_version(
        &self,
        conversation_id: String,
        assistant_message_id: String,
        answer_version_id: String,
        request_id: String,
    ) -> BridgeResult<Value> {
        self.ensure_started()?;
        conversation(self.request_started(
            "workbench.select_answer_version",
            json!({
                "conversation_id": conversation_id,
                "assistant_message_id": assistant_message_id,
                "answer_version_id": answer_version_id,
            }),
            Some(request_id),
        )?)
    }
}
