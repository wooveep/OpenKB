//! Typed retrieval, answer, and immutable retrieval-trace wire values.

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum AnswerStatus {
    Completed,
    Interrupted,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RetrievalPlan {
    pub query: String,
    pub terms: Vec<String>,
    pub source: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EvidenceRef {
    #[serde(alias = "evidence_id")]
    pub evidence_id: String,
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub section: String,
    pub locator: Value,
    pub excerpt: String,
    pub channels: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RetrievalChannelTrace {
    pub channel: String,
    #[serde(default, alias = "candidate_count")]
    pub candidate_count: u64,
    #[serde(default, alias = "trigger_reasons")]
    pub trigger_reasons: Vec<String>,
    #[serde(default, alias = "degradation_reasons")]
    pub degradation_reasons: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RetrievalTrace {
    #[serde(default, alias = "catalog_generation_ids")]
    pub catalog_generation_ids: Vec<String>,
    #[serde(default, alias = "page_tree_generation_ids")]
    pub page_tree_generation_ids: Vec<String>,
    #[serde(default)]
    pub channels: Vec<RetrievalChannelTrace>,
    #[serde(default, alias = "trigger_reasons")]
    pub trigger_reasons: Vec<String>,
    #[serde(default, alias = "degradation_reasons")]
    pub degradation_reasons: Vec<String>,
    #[serde(default, alias = "selected_node_ids")]
    pub selected_node_ids: Vec<String>,
    #[serde(default, alias = "canonical_evidence_ids")]
    pub canonical_evidence_ids: Vec<String>,
    #[serde(default, alias = "fusion_policy_version")]
    pub fusion_policy_version: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GroundedAnswer {
    #[serde(alias = "answer_id")]
    pub answer_id: String,
    pub question: String,
    #[serde(alias = "answer_text")]
    pub answer_text: String,
    #[serde(alias = "retrieval_plan")]
    pub retrieval_plan: RetrievalPlan,
    pub citations: Vec<EvidenceRef>,
    #[serde(default, alias = "source_images")]
    pub source_images: Vec<AnswerSourceImage>,
    #[serde(default, alias = "retrieval_trace")]
    pub retrieval_trace: RetrievalTrace,
    #[serde(default)]
    pub degradations: Vec<String>,
    #[serde(default = "default_completed_answer_status")]
    pub status: AnswerStatus,
    #[serde(default, alias = "interruption_code")]
    pub interruption_code: Option<String>,
    #[serde(default, alias = "interruption_reason")]
    pub interruption_reason: Option<String>,
    #[serde(alias = "created_at")]
    pub created_at: String,
}

fn default_completed_answer_status() -> AnswerStatus {
    AnswerStatus::Completed
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AnswerSourceImage {
    #[serde(alias = "source_image_id")]
    pub source_image_id: String,
    #[serde(alias = "evidence_id")]
    pub evidence_id: String,
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub name: String,
    #[serde(alias = "media_type")]
    pub media_type: String,
    #[serde(alias = "file_path")]
    pub file_path: String,
    #[serde(alias = "alt_text")]
    pub alt_text: Option<String>,
    pub locator: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GroundedAnswersResult {
    pub answers: Vec<GroundedAnswer>,
}
