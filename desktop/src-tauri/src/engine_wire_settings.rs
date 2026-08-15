//! Non-secret model defaults and user-exported diagnostic bundle wire values.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelSettings {
    pub model: String,
    #[serde(alias = "credential_reference")]
    pub credential_reference: String,
    #[serde(alias = "credential_available")]
    pub credential_available: bool,
    #[serde(alias = "max_concurrent_model_calls")]
    pub max_concurrent_model_calls: u32,
    #[serde(alias = "initial_timeout_seconds")]
    pub initial_timeout_seconds: f64,
    #[serde(alias = "model_call_deadline_seconds")]
    pub model_call_deadline_seconds: f64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DiagnosticBundleResult {
    pub path: String,
    pub files: Vec<String>,
}
