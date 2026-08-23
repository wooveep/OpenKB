//! Wire-safe lifecycle values for Model Calls governed by explicit terminal events.

use serde::{Deserialize, Serialize};

use super::import_observability::{ModelExecutionLane, ModelRole};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelCallLifecycleEventData {
    #[serde(alias = "request_id")]
    pub request_id: String,
    #[serde(alias = "call_id")]
    pub call_id: String,
    pub attempt: u64,
    pub status: ModelCallLifecycleStatus,
    #[serde(alias = "elapsed_seconds")]
    pub elapsed_seconds: f64,
    #[serde(alias = "failure_code")]
    pub failure_code: Option<String>,
    pub reason: Option<String>,
    #[serde(alias = "retry_after_seconds")]
    pub retry_after_seconds: Option<f64>,
    pub operation: String,
    #[serde(alias = "model_role")]
    pub model_role: ModelRole,
    pub provider: String,
    #[serde(alias = "model_name")]
    pub model_name: String,
    #[serde(alias = "execution_lane")]
    pub execution_lane: ModelExecutionLane,
    #[serde(alias = "attempt_id")]
    pub attempt_id: String,
    #[serde(alias = "long_wait_threshold_seconds")]
    pub long_wait_threshold_seconds: f64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelCallLifecycleStatus {
    Queued,
    Connecting,
    AwaitingModelResult,
    ModelOutputActivity,
    Validating,
    Completed,
    Retrying,
    Cancelled,
    ProviderFailure,
    NetworkFailure,
}
