//! Typed, content-free import progress and model observability wire values.

use super::{
    ImportStageStatus, ModelCallLifecycleStatus, ModelUsageAggregate, ParserFamily,
    ParserResourceState, ParserRoute, ParserRuntimeState,
};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImportProgressStage {
    Preflight,
    RawAsset,
    ParserInitialization,
    DocumentIr,
    Evidence,
    KnowledgeAnalysisPlan,
    KnowledgeAnalysisBatches,
    KnowledgeAnalysisMerge,
    Publication,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImportRuntimeKind {
    Parser,
    Model,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelRole {
    Default,
    Analysis,
    Answer,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelExecutionLane {
    Background,
    Interactive,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TokenUsageSource {
    ProviderReported,
    Estimated,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelActivityStatus {
    Queued,
    Connecting,
    AwaitingFirstResult,
    ReceivingOutput,
    Validating,
    Retrying,
    Completed,
    Interrupted,
    ProviderFailure,
    NetworkFailure,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelActivityAction {
    Cancel,
    Resume,
    Retry,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LegacyModelRecoveryKind {
    LegacyModelDeadline,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LegacyModelRecoveryAction {
    ContinueCompatible,
    RestartCurrentPlan,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportProgressStep {
    pub stage: ImportProgressStage,
    pub status: ImportStageStatus,
    #[serde(alias = "source_stage_run_id")]
    pub source_stage_run_id: String,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
    #[serde(default, alias = "runtime_kind")]
    pub runtime_kind: Option<ImportRuntimeKind>,
    #[serde(default, alias = "parser_family")]
    pub parser_family: Option<ParserFamily>,
    #[serde(default, alias = "parser_route")]
    pub parser_route: Option<ParserRoute>,
    #[serde(default, alias = "parser_resource_state")]
    pub parser_resource_state: Option<ParserResourceState>,
    #[serde(default, alias = "parser_runtime_state")]
    pub parser_runtime_state: Option<ParserRuntimeState>,
    #[serde(default)]
    pub completed: Option<u32>,
    #[serde(default)]
    pub total: Option<u32>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelUsageRecord {
    #[serde(alias = "call_id")]
    pub call_id: String,
    pub attempt: u32,
    #[serde(alias = "attempt_id")]
    pub attempt_id: String,
    pub operation: String,
    #[serde(alias = "model_role")]
    pub model_role: ModelRole,
    pub provider: String,
    pub model: String,
    #[serde(alias = "job_id")]
    pub job_id: Option<String>,
    #[serde(alias = "stage_run_id")]
    pub stage_run_id: Option<String>,
    #[serde(alias = "batch_id")]
    pub batch_id: Option<String>,
    #[serde(alias = "execution_lane")]
    pub execution_lane: ModelExecutionLane,
    #[serde(alias = "lifecycle_status")]
    pub lifecycle_status: ModelCallLifecycleStatus,
    #[serde(alias = "failure_code")]
    pub failure_code: Option<String>,
    #[serde(alias = "queue_seconds")]
    pub queue_seconds: Option<f64>,
    #[serde(alias = "connect_seconds")]
    pub connect_seconds: Option<f64>,
    #[serde(alias = "first_output_seconds")]
    pub first_output_seconds: Option<f64>,
    #[serde(alias = "total_seconds")]
    pub total_seconds: Option<f64>,
    #[serde(alias = "input_tokens")]
    pub input_tokens: Option<u64>,
    #[serde(alias = "output_tokens")]
    pub output_tokens: Option<u64>,
    #[serde(alias = "total_tokens")]
    pub total_tokens: Option<u64>,
    #[serde(alias = "token_usage_source")]
    pub token_usage_source: Option<TokenUsageSource>,
    #[serde(alias = "input_cost")]
    pub input_cost: Option<f64>,
    #[serde(alias = "output_cost")]
    pub output_cost: Option<f64>,
    #[serde(alias = "total_cost")]
    pub total_cost: Option<f64>,
    #[serde(alias = "provider_request_id")]
    pub provider_request_id: Option<String>,
    #[serde(alias = "created_at")]
    pub created_at: String,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelActivity {
    pub operation: String,
    #[serde(alias = "model_role")]
    pub model_role: ModelRole,
    pub provider: String,
    pub model: String,
    #[serde(alias = "call_id")]
    pub call_id: String,
    pub attempt: u32,
    #[serde(alias = "attempt_id")]
    pub attempt_id: String,
    #[serde(alias = "batch_id")]
    pub batch_id: Option<String>,
    #[serde(alias = "execution_lane")]
    pub execution_lane: ModelExecutionLane,
    pub status: ModelActivityStatus,
    #[serde(alias = "failure_code")]
    pub failure_code: Option<String>,
    #[serde(alias = "elapsed_seconds")]
    pub elapsed_seconds: f64,
    #[serde(alias = "long_wait_advisory")]
    pub long_wait_advisory: bool,
    #[serde(alias = "long_wait_threshold_seconds")]
    pub long_wait_threshold_seconds: f64,
    #[serde(alias = "available_actions")]
    pub available_actions: Vec<ModelActivityAction>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LegacyModelRecoveryChoice {
    pub allowed: bool,
    #[serde(alias = "estimated_remaining_calls")]
    pub estimated_remaining_calls: u32,
    #[serde(alias = "estimated_input_tokens")]
    pub estimated_input_tokens: u64,
    #[serde(default, alias = "reuses_completed_batches")]
    pub reuses_completed_batches: Option<u32>,
    #[serde(default, alias = "reuses_parser_document_ir_evidence")]
    pub reuses_parser_document_ir_evidence: Option<bool>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct LegacyModelRecoveryChoices {
    pub continue_compatible: LegacyModelRecoveryChoice,
    pub restart_current_plan: LegacyModelRecoveryChoice,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LegacyModelRecovery {
    pub kind: LegacyModelRecoveryKind,
    pub compatible: bool,
    #[serde(alias = "compatibility_reason")]
    pub compatibility_reason: String,
    #[serde(alias = "previous_prompt_digest")]
    pub previous_prompt_digest: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    #[serde(alias = "completed_batches")]
    pub completed_batches: u32,
    #[serde(alias = "total_batches")]
    pub total_batches: u32,
    pub choices: LegacyModelRecoveryChoices,
    #[serde(alias = "recommended_choice")]
    pub recommended_choice: LegacyModelRecoveryAction,
    #[serde(alias = "selected_choice")]
    pub selected_choice: Option<LegacyModelRecoveryAction>,
    #[serde(alias = "starts_automatically")]
    pub starts_automatically: bool,
}

pub type ImportUsageAggregate = ModelUsageAggregate;
