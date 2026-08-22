//! Wire-safe lifecycle values for Model Calls governed by explicit terminal events.

use serde::{Deserialize, Serialize};

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
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelCallLifecycleStatus {
    Queued,
    Connecting,
    AwaitingModelResult,
    ModelOutputActivity,
    Completed,
    Retrying,
    Cancelled,
    ProviderFailure,
    NetworkFailure,
}
