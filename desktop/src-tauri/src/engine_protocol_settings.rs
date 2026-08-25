//! Model-settings and diagnostic-bundle requests for the private Engine transport.

use super::{
    validated_response, BridgeError, BridgeResult, DiagnosticBundleResult, EngineSupervisor,
    ModelSettings, ModelSettingsDraft,
};
use serde::Deserialize;
use serde_json::{json, Value};

#[allow(dead_code)]
#[derive(Deserialize)]
struct ModelConnectionTest {
    ok: bool,
    model: String,
    latency_ms: u64,
    attempt_count: u64,
}

impl EngineSupervisor {
    pub fn model_settings(&self) -> BridgeResult<ModelSettings> {
        self.request_model_settings("workbench.model_settings", json!({}), None)
    }

    pub fn save_model_settings(
        &self,
        settings: ModelSettingsDraft,
        request_id: String,
    ) -> BridgeResult<ModelSettings> {
        self.request_model_settings(
            "workbench.save_model_settings",
            model_settings_params(&settings),
            Some(request_id),
        )
    }

    pub fn test_model_connection(
        &self,
        settings: ModelSettingsDraft,
        request_id: String,
    ) -> BridgeResult<Value> {
        self.ensure_started()?;
        let value = self.request_started_with_wait(
            "workbench.test_model_connection",
            model_settings_params(&settings),
            Some(request_id),
            None,
        )?;
        validated_response::<ModelConnectionTest>(value, "model connection test")
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

fn model_settings_params(settings: &ModelSettingsDraft) -> Value {
    json!({
        "provider": settings.provider,
        "model": settings.model,
        "api_base_url": settings.api_base_url,
        "api_key": settings.api_key,
        "max_concurrent_model_calls": settings.max_concurrent_model_calls,
        "requests_per_minute": settings.requests_per_minute,
        "tokens_per_minute": settings.tokens_per_minute,
        "analysis_model": settings.analysis_model,
        "answer_model": settings.answer_model,
        "default_context_capacity": settings.default_context_capacity,
        "analysis_context_capacity": settings.analysis_context_capacity,
        "answer_context_capacity": settings.answer_context_capacity,
        "default_reasoning": settings.default_reasoning,
        "analysis_reasoning": settings.analysis_reasoning,
        "answer_reasoning": settings.answer_reasoning,
        "default_input_price_per_million": settings.default_input_price_per_million,
        "default_output_price_per_million": settings.default_output_price_per_million,
        "analysis_input_price_per_million": settings.analysis_input_price_per_million,
        "analysis_output_price_per_million": settings.analysis_output_price_per_million,
        "answer_input_price_per_million": settings.answer_input_price_per_million,
        "answer_output_price_per_million": settings.answer_output_price_per_million,
    })
}

#[cfg(test)]
mod tests {
    use super::{model_settings_params, ModelSettingsDraft};
    use serde_json::json;

    #[test]
    fn engine_params_are_snake_case_and_have_no_legacy_timeout() {
        let draft: ModelSettingsDraft = serde_json::from_value(json!({
            "provider": "custom",
            "model": "default-model",
            "apiBaseUrl": "https://models.example.test/v1",
            "apiKey": "secret",
            "maxConcurrentModelCalls": 2,
            "requestsPerMinute": 120,
            "tokensPerMinute": 240000,
            "analysisModel": "analysis-model",
            "answerModel": null,
            "defaultContextCapacity": 65536,
            "analysisContextCapacity": null,
            "answerContextCapacity": null,
            "defaultReasoning": "medium",
            "analysisReasoning": null,
            "answerReasoning": null,
            "defaultInputPricePerMillion": 1.0,
            "defaultOutputPricePerMillion": 2.0,
            "analysisInputPricePerMillion": null,
            "analysisOutputPricePerMillion": null,
            "answerInputPricePerMillion": null,
            "answerOutputPricePerMillion": null
        }))
        .expect("Tauri draft should deserialize");

        let params = model_settings_params(&draft);
        assert_eq!(params["analysis_model"], "analysis-model");
        assert_eq!(params["default_context_capacity"], 65536);
        assert_eq!(params["requests_per_minute"], 120);
        assert_eq!(params["tokens_per_minute"], 240000);
        assert!(params.get("initial_timeout_seconds").is_none());
        assert!(params.get("model_call_deadline_seconds").is_none());
    }
}
