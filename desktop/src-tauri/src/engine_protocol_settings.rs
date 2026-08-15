//! Model-settings and diagnostic-bundle requests for the private Engine transport.

use super::{BridgeError, BridgeResult, DiagnosticBundleResult, EngineSupervisor, ModelSettings};
use serde_json::json;

impl EngineSupervisor {
    pub fn model_settings(&self) -> BridgeResult<ModelSettings> {
        self.request_model_settings("workbench.model_settings", json!({}), None)
    }

    pub fn save_model_settings(
        &self,
        model: String,
        credential_reference: String,
        max_concurrent_model_calls: u32,
        initial_timeout_seconds: f64,
        request_id: String,
    ) -> BridgeResult<ModelSettings> {
        self.request_model_settings(
            "workbench.save_model_settings",
            json!({
                "model": model,
                "credential_reference": credential_reference,
                "max_concurrent_model_calls": max_concurrent_model_calls,
                "initial_timeout_seconds": initial_timeout_seconds,
            }),
            Some(request_id),
        )
    }

    pub fn export_diagnostic_bundle(
        &self,
        destination: String,
        request_id: String,
    ) -> BridgeResult<DiagnosticBundleResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.export_diagnostic_bundle",
            json!({ "destination": destination }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine diagnostic-bundle response has an invalid shape: {error}"),
            )
        })
    }

    fn request_model_settings(
        &self,
        method: &str,
        params: serde_json::Value,
        request_id: Option<String>,
    ) -> BridgeResult<ModelSettings> {
        self.ensure_started()?;
        let value = self.request_started(method, params, request_id)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine model-settings response has an invalid shape: {error}"),
            )
        })
    }
}
