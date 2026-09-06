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
    #[serde(default, alias = "version_label")]
    pub version_label: Option<String>,
    #[serde(default, alias = "version_side")]
    pub version_side: Option<String>,
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

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct QuestionFacetTrace {
    #[serde(alias = "facet_id")]
    pub facet_id: String,
    pub label: String,
    pub description: String,
    pub importance: FacetImportance,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FacetImportance {
    Required,
    Supporting,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FacetCoverageTrace {
    #[serde(alias = "facet_id")]
    pub facet_id: String,
    pub state: FacetCoverageState,
    #[serde(default, alias = "evidence_ids")]
    pub evidence_ids: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FacetCoverageState {
    Covered,
    Partial,
    Missing,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SemanticStructureState {
    Known,
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
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
    #[serde(default, alias = "navigation_snapshot_ids")]
    pub navigation_snapshot_ids: Vec<String>,
    #[serde(default, alias = "navigation_routes")]
    pub navigation_routes: Vec<String>,
    #[serde(default, alias = "navigation_read_count")]
    pub navigation_read_count: u64,
    #[serde(default, alias = "source_window_count")]
    pub source_window_count: u64,
    #[serde(default, alias = "link_hop_count")]
    pub link_hop_count: u64,
    #[serde(default, alias = "page_tree_supplement_count")]
    pub page_tree_supplement_count: u64,
    #[serde(alias = "semantic_structure_state")]
    pub semantic_structure_state: SemanticStructureState,
    #[serde(alias = "question_goal")]
    pub question_goal: String,
    #[serde(alias = "question_facets")]
    pub question_facets: Vec<QuestionFacetTrace>,
    #[serde(alias = "question_facet_plan_digest")]
    pub question_facet_plan_digest: String,
    #[serde(alias = "query_planning_prompt_contract_digest")]
    pub query_planning_prompt_contract_digest: String,
    #[serde(alias = "query_planning_execution_profile_json")]
    pub query_planning_execution_profile_json: String,
    #[serde(alias = "query_planning_execution_profile_digest")]
    pub query_planning_execution_profile_digest: String,
    #[serde(alias = "facet_coverage")]
    pub facet_coverage: Vec<FacetCoverageTrace>,
    #[serde(alias = "coverage_gate_state")]
    pub coverage_gate_state: String,
    #[serde(default, alias = "navigation_round_count")]
    pub navigation_round_count: u64,
    #[serde(default, alias = "navigation_action_kinds")]
    pub navigation_action_kinds: Vec<String>,
    #[serde(default, alias = "navigation_stop_reason")]
    pub navigation_stop_reason: String,
    #[serde(default, alias = "navigation_model_calls")]
    pub navigation_model_calls: u64,
    #[serde(default, alias = "navigation_logical_read_count")]
    pub navigation_logical_read_count: u64,
    #[serde(default, alias = "navigation_source_tokens")]
    pub navigation_source_tokens: u64,
    #[serde(default, alias = "grounding_input_budget_tokens")]
    pub grounding_input_budget_tokens: u64,
    #[serde(default, alias = "evidence_input_tokens")]
    pub evidence_input_tokens: u64,
    #[serde(default, alias = "guidance_input_tokens")]
    pub guidance_input_tokens: u64,
    #[serde(default, alias = "version_navigation_snapshot_id")]
    pub version_navigation_snapshot_id: String,
    #[serde(default, alias = "version_catalog_revision_id")]
    pub version_catalog_revision_id: String,
    #[serde(default, alias = "version_catalog_digest")]
    pub version_catalog_digest: String,
    #[serde(default, alias = "version_scope_mode")]
    pub version_scope_mode: String,
    #[serde(default, alias = "version_scope_status")]
    pub version_scope_status: String,
    #[serde(default, alias = "version_scope_lineage_ids")]
    pub version_scope_lineage_ids: Vec<String>,
    #[serde(default, alias = "version_scope_labels")]
    pub version_scope_labels: Vec<String>,
    #[serde(default, alias = "version_scope_document_ids")]
    pub version_scope_document_ids: Vec<String>,
    #[serde(default, alias = "version_scope_selection_reason")]
    pub version_scope_selection_reason: String,
    #[serde(default, alias = "version_scope_degradation_reason")]
    pub version_scope_degradation_reason: String,
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
    #[serde(alias = "retrieval_trace")]
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
