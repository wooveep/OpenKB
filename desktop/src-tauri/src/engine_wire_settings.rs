//! Model settings and user-exported diagnostic bundle wire values.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelSettingsDraft {
    #[serde(default = "default_model_provider")]
    pub provider: String,
    pub model: String,
    #[serde(alias = "api_base_url")]
    pub api_base_url: String,
    #[serde(alias = "api_key")]
    pub api_key: String,
    #[serde(alias = "max_concurrent_model_calls")]
    pub max_concurrent_model_calls: u32,
    #[serde(default, alias = "analysis_model")]
    pub analysis_model: Option<String>,
    #[serde(default, alias = "answer_model")]
    pub answer_model: Option<String>,
    #[serde(default, alias = "default_context_capacity")]
    pub default_context_capacity: Option<u64>,
    #[serde(default, alias = "analysis_context_capacity")]
    pub analysis_context_capacity: Option<u64>,
    #[serde(default, alias = "answer_context_capacity")]
    pub answer_context_capacity: Option<u64>,
    #[serde(default, alias = "default_reasoning")]
    pub default_reasoning: Option<String>,
    #[serde(default, alias = "analysis_reasoning")]
    pub analysis_reasoning: Option<String>,
    #[serde(default, alias = "answer_reasoning")]
    pub answer_reasoning: Option<String>,
    #[serde(default, alias = "default_input_price_per_million")]
    pub default_input_price_per_million: Option<f64>,
    #[serde(default, alias = "default_output_price_per_million")]
    pub default_output_price_per_million: Option<f64>,
    #[serde(default, alias = "analysis_input_price_per_million")]
    pub analysis_input_price_per_million: Option<f64>,
    #[serde(default, alias = "analysis_output_price_per_million")]
    pub analysis_output_price_per_million: Option<f64>,
    #[serde(default, alias = "answer_input_price_per_million")]
    pub answer_input_price_per_million: Option<f64>,
    #[serde(default, alias = "answer_output_price_per_million")]
    pub answer_output_price_per_million: Option<f64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelSettings {
    #[serde(flatten)]
    pub draft: ModelSettingsDraft,
    #[serde(alias = "api_key_configured")]
    pub api_key_configured: bool,
    #[serde(alias = "analysis_concurrency")]
    pub analysis_concurrency: u32,
    #[serde(alias = "usage_aggregate")]
    pub usage_aggregate: ModelUsageAggregate,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelUsageAggregate {
    #[serde(alias = "call_count")]
    pub call_count: u64,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u64,
    #[serde(alias = "failure_count")]
    pub failure_count: u64,
    #[serde(alias = "input_tokens")]
    pub input_tokens: u64,
    #[serde(alias = "output_tokens")]
    pub output_tokens: u64,
    #[serde(alias = "total_tokens")]
    pub total_tokens: u64,
    #[serde(alias = "total_cost")]
    pub total_cost: Option<f64>,
    #[serde(default, alias = "token_usage_source")]
    pub token_usage_source: Option<AggregateTokenUsageSource>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AggregateTokenUsageSource {
    ProviderReported,
    Estimated,
    Mixed,
}

fn default_model_provider() -> String {
    "custom".to_owned()
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DiagnosticBundleResult {
    pub path: String,
    pub files: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::ModelSettings;
    use serde_json::{json, Value};

    #[test]
    fn current_model_settings_have_roles_usage_and_no_response_timeout() {
        let settings: ModelSettings = serde_json::from_value(json!({
            "provider": "custom",
            "model": "default-model",
            "api_base_url": "https://models.example.test/v1",
            "api_key": "secret",
            "api_key_configured": true,
            "max_concurrent_model_calls": 2,
            "analysis_model": "analysis-model",
            "answer_model": "answer-model",
            "analysis_concurrency": 2,
            "default_context_capacity": 65536,
            "analysis_context_capacity": 32768,
            "answer_context_capacity": null,
            "default_reasoning": "medium",
            "analysis_reasoning": "high",
            "answer_reasoning": "off",
            "default_input_price_per_million": 1.5,
            "default_output_price_per_million": 3.0,
            "analysis_input_price_per_million": null,
            "analysis_output_price_per_million": null,
            "answer_input_price_per_million": 2.0,
            "answer_output_price_per_million": 4.0,
            "usage_aggregate": {
                "call_count": 2,
                "attempt_count": 3,
                "failure_count": 1,
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "total_cost": 0.00023,
                "token_usage_source": "provider_reported"
            }
        }))
        .expect("current Engine model settings should deserialize");

        let encoded = serde_json::to_value(settings).expect("settings should serialize");
        let object = encoded.as_object().expect("settings should stay an object");
        assert_eq!(
            encoded["analysisModel"],
            Value::String("analysis-model".into())
        );
        assert_eq!(encoded["usageAggregate"]["callCount"], 2);
        assert!(!object.contains_key("initialTimeoutSeconds"));
        assert!(!object.contains_key("modelCallDeadlineSeconds"));
    }
}
