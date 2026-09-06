//! Model settings and user-exported diagnostic bundle wire values.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StructuredOutputMode {
    JsonSchema,
    JsonObject,
    PromptContract,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelReasoningMode {
    Off,
    Low,
    Medium,
    High,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelReasoningSource {
    ExplicitRole,
    InheritedDefault,
    AnalysisSafeDefault,
    ProviderDefault,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelCapabilityStatus {
    Unchecked,
    Checking,
    Verified,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelCapabilityCheckStatus {
    Verified,
    AnswerVerified,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelCapabilityCheckRoleStatus {
    Verified,
    Unavailable,
    Failed,
    Cancelled,
    Unverified,
    NotRequired,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelCapabilityCheckRole {
    Default,
    Analysis,
    Answer,
}

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
    #[serde(default, alias = "requests_per_minute")]
    pub requests_per_minute: Option<u64>,
    #[serde(default, alias = "tokens_per_minute")]
    pub tokens_per_minute: Option<u64>,
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
    pub default_reasoning: Option<ModelReasoningMode>,
    #[serde(default, alias = "analysis_reasoning")]
    pub analysis_reasoning: Option<ModelReasoningMode>,
    #[serde(default, alias = "answer_reasoning")]
    pub answer_reasoning: Option<ModelReasoningMode>,
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
    #[serde(alias = "provider_adapter")]
    pub provider_adapter: ModelProviderAdapter,
    #[serde(alias = "effective_roles")]
    pub effective_roles: EffectiveModelRoles,
    #[serde(alias = "analysis_capability")]
    pub analysis_capability: ModelCapabilityState,
    #[serde(alias = "answer_capability")]
    pub answer_capability: ModelCapabilityState,
    #[serde(alias = "usage_aggregate")]
    pub usage_aggregate: ModelUsageAggregate,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelProviderAdapter {
    pub identity: String,
    pub version: String,
    #[serde(alias = "structured_output_mode")]
    pub structured_output_mode: Option<StructuredOutputMode>,
    #[serde(alias = "supports_structured_analysis")]
    pub supports_structured_analysis: bool,
    #[serde(alias = "supported_reasoning")]
    pub supported_reasoning: Vec<ModelReasoningMode>,
    #[serde(alias = "analysis_unavailable_reason")]
    pub analysis_unavailable_reason: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EffectiveModelRoles {
    pub default: EffectiveModelRole,
    pub analysis: EffectiveModelRole,
    pub answer: EffectiveModelRole,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EffectiveModelRole {
    pub model: String,
    #[serde(alias = "context_capacity")]
    pub context_capacity: u64,
    pub reasoning: Option<ModelReasoningMode>,
    #[serde(alias = "reasoning_source")]
    pub reasoning_source: ModelReasoningSource,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelCapabilityState {
    #[serde(alias = "profile_identity")]
    pub profile_identity: Option<String>,
    pub status: ModelCapabilityStatus,
    #[serde(alias = "failure_code")]
    pub failure_code: Option<String>,
    pub reason: Option<String>,
    #[serde(alias = "checked_at")]
    pub checked_at: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelCapabilityCheckRoleResult {
    pub role: ModelCapabilityCheckRole,
    pub model: Option<String>,
    pub status: ModelCapabilityCheckRoleStatus,
    pub reason: Option<String>,
    #[serde(default, alias = "failure_code")]
    pub failure_code: Option<String>,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u64,
    #[serde(alias = "profile_identity")]
    pub profile_identity: Option<String>,
    pub cached: bool,
    #[serde(default, alias = "covered_by")]
    pub covered_by: Option<ModelCapabilityCheckRole>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelCapabilityCheckRoleResults {
    pub default: ModelCapabilityCheckRoleResult,
    pub analysis: ModelCapabilityCheckRoleResult,
    pub answer: ModelCapabilityCheckRoleResult,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ModelCapabilityCheckRoleResultsWire {
    default: ModelCapabilityCheckRoleResult,
    analysis: ModelCapabilityCheckRoleResult,
    answer: ModelCapabilityCheckRoleResult,
}

impl<'de> Deserialize<'de> for ModelCapabilityCheckRoleResults {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let wire = ModelCapabilityCheckRoleResultsWire::deserialize(deserializer)?;
        if wire.default.role != ModelCapabilityCheckRole::Default {
            return Err(<D::Error as serde::de::Error>::custom(
                "roleResults.default must contain role=default",
            ));
        }
        if wire.analysis.role != ModelCapabilityCheckRole::Analysis {
            return Err(<D::Error as serde::de::Error>::custom(
                "roleResults.analysis must contain role=analysis",
            ));
        }
        if wire.answer.role != ModelCapabilityCheckRole::Answer {
            return Err(<D::Error as serde::de::Error>::custom(
                "roleResults.answer must contain role=answer",
            ));
        }
        Ok(Self {
            default: wire.default,
            analysis: wire.analysis,
            answer: wire.answer,
        })
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelConnectionTest {
    pub ok: bool,
    pub model: String,
    #[serde(default)]
    pub models: Vec<String>,
    #[serde(alias = "latency_ms")]
    pub latency_ms: u64,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u64,
    #[serde(alias = "profile_identity")]
    pub profile_identity: String,
    #[serde(alias = "capability_status")]
    pub capability_status: ModelCapabilityCheckStatus,
    #[serde(alias = "role_results")]
    pub role_results: ModelCapabilityCheckRoleResults,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SaveAndVerifyModelConfiguration {
    pub saved: bool,
    #[serde(alias = "verification_cost_accepted")]
    pub verification_cost_accepted: bool,
    #[serde(alias = "all_required_roles_verified")]
    pub all_required_roles_verified: bool,
    pub cancelled: bool,
    #[serde(default)]
    pub models: Vec<String>,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u64,
    #[serde(alias = "latency_ms")]
    pub latency_ms: u64,
    #[serde(alias = "role_results")]
    pub role_results: ModelCapabilityCheckRoleResults,
    pub settings: ModelSettings,
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
    use super::{
        ModelCapabilityCheckRoleStatus, ModelConnectionTest, ModelSettings,
        SaveAndVerifyModelConfiguration,
    };
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
            "requests_per_minute": 120,
            "tokens_per_minute": 240000,
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
            "provider_adapter": {
                "identity": "deepseek",
                "version": "deepseek.v1",
                "structured_output_mode": "json_object",
                "supports_structured_analysis": true,
                "supported_reasoning": ["high", "low", "medium", "off"],
                "analysis_unavailable_reason": null
            },
            "effective_roles": {
                "default": {"model": "default-model", "context_capacity": 65536, "reasoning": "medium", "reasoning_source": "explicit_role"},
                "analysis": {"model": "analysis-model", "context_capacity": 32768, "reasoning": "high", "reasoning_source": "explicit_role"},
                "answer": {"model": "answer-model", "context_capacity": 65536, "reasoning": "off", "reasoning_source": "explicit_role"}
            },
            "analysis_capability": {
                "profile_identity": "profile-1",
                "status": "verified",
                "failure_code": null,
                "reason": null,
                "checked_at": "2026-08-26T00:00:00+00:00"
            },
            "answer_capability": {
                "profile_identity": "answer-profile-1",
                "status": "verified",
                "failure_code": null,
                "reason": null,
                "checked_at": "2026-08-26T00:00:00+00:00"
            },
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

        let encoded = serde_json::to_value(&settings).expect("settings should serialize");
        let object = encoded.as_object().expect("settings should stay an object");
        assert_eq!(
            encoded["analysisModel"],
            Value::String("analysis-model".into())
        );
        assert_eq!(encoded["usageAggregate"]["callCount"], 2);
        assert_eq!(encoded["requestsPerMinute"], 120);
        assert_eq!(encoded["tokensPerMinute"], 240000);
        assert_eq!(settings.provider_adapter.identity, "deepseek");
        assert!(matches!(
            settings.effective_roles.analysis.reasoning,
            Some(super::ModelReasoningMode::High)
        ));
        assert!(matches!(
            settings.analysis_capability.status,
            super::ModelCapabilityStatus::Verified
        ));
        assert!(!object.contains_key("initialTimeoutSeconds"));
        assert!(!object.contains_key("modelCallDeadlineSeconds"));
    }

    #[test]
    fn model_settings_reject_unknown_closed_configuration_values() {
        let valid = json!({
            "provider": "custom",
            "model": "default-model",
            "api_base_url": "https://models.example.test/v1",
            "api_key": "",
            "api_key_configured": true,
            "max_concurrent_model_calls": 2,
            "analysis_concurrency": 2,
            "provider_adapter": {
                "identity": "deepseek",
                "version": "deepseek.v1",
                "structured_output_mode": "json_object",
                "supports_structured_analysis": true,
                "supported_reasoning": ["off", "low", "medium", "high"],
                "analysis_unavailable_reason": null
            },
            "effective_roles": {
                "default": {"model": "default-model", "context_capacity": 65536, "reasoning": null, "reasoning_source": "provider_default"},
                "analysis": {"model": "analysis-model", "context_capacity": 32768, "reasoning": "high", "reasoning_source": "explicit_role"},
                "answer": {"model": "answer-model", "context_capacity": 65536, "reasoning": "off", "reasoning_source": "inherited_default"}
            },
            "analysis_capability": {
                "profile_identity": "profile-1",
                "status": "verified",
                "failure_code": null,
                "reason": null,
                "checked_at": "2026-08-26T00:00:00+00:00"
            },
            "answer_capability": {
                "profile_identity": "answer-profile-1",
                "status": "verified",
                "failure_code": null,
                "reason": null,
                "checked_at": "2026-08-26T00:00:00+00:00"
            },
            "usage_aggregate": {
                "call_count": 0,
                "attempt_count": 0,
                "failure_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "total_cost": null
            }
        });

        for path in [
            &["provider_adapter", "structured_output_mode"][..],
            &["provider_adapter", "supported_reasoning", "0"][..],
            &["effective_roles", "analysis", "reasoning"][..],
            &["effective_roles", "analysis", "reasoning_source"][..],
            &["analysis_capability", "status"][..],
            &["analysis_reasoning"][..],
        ] {
            let mut candidate = valid.clone();
            let mut value = &mut candidate;
            for segment in &path[..path.len() - 1] {
                value = if let Ok(index) = segment.parse::<usize>() {
                    &mut value[index]
                } else {
                    &mut value[*segment]
                };
            }
            let leaf = path[path.len() - 1];
            if let Ok(index) = leaf.parse::<usize>() {
                value[index] = json!("invented_value");
            } else {
                value[leaf] = json!("invented_value");
            }
            assert!(
                serde_json::from_value::<ModelSettings>(candidate).is_err(),
                "unknown closed value at {path:?} must fail the Desktop Bridge boundary"
            );
        }
    }

    #[test]
    fn model_connection_rejects_unknown_capability_status_values() {
        let valid = json!({
            "ok": true,
            "model": "answer-model",
            "models": ["answer-model"],
            "latency_ms": 8,
            "attempt_count": 1,
            "profile_identity": "answer-profile-1",
            "capability_status": "answer_verified",
            "role_results": {
                "default": {
                    "role": "default",
                    "model": "answer-model",
                    "status": "verified",
                    "attempt_count": 1,
                    "profile_identity": "answer-profile-1",
                    "cached": false,
                    "covered_by": "answer"
                },
                "analysis": {
                    "role": "analysis",
                    "status": "unavailable",
                    "reason": "No named Analysis adapter.",
                    "attempt_count": 0,
                    "profile_identity": null,
                    "cached": false
                },
                "answer": {
                    "role": "answer",
                    "model": "answer-model",
                    "status": "verified",
                    "attempt_count": 1,
                    "profile_identity": "answer-profile-1",
                    "cached": false
                }
            }
        });
        serde_json::from_value::<ModelConnectionTest>(valid.clone())
            .expect("known capability-check values should deserialize");

        for path in [
            &["capability_status"][..],
            &["role_results", "answer", "status"][..],
            &["role_results", "answer", "role"][..],
        ] {
            let mut candidate = valid.clone();
            let mut value = &mut candidate;
            for segment in &path[..path.len() - 1] {
                value = &mut value[*segment];
            }
            value[path[path.len() - 1]] = json!("invented_value");
            assert!(
                serde_json::from_value::<ModelConnectionTest>(candidate).is_err(),
                "unknown capability-check value at {path:?} must fail the Bridge boundary"
            );
        }

        let mut invented_role = valid.clone();
        invented_role["role_results"]["invented"] = valid["role_results"]["answer"].clone();
        assert!(
            serde_json::from_value::<ModelConnectionTest>(invented_role).is_err(),
            "invented role-result keys must fail the Bridge boundary"
        );

        let mut mismatched_role = valid.clone();
        mismatched_role["role_results"]["default"]["role"] = json!("answer");
        assert!(
            serde_json::from_value::<ModelConnectionTest>(mismatched_role).is_err(),
            "role-result keys and nested role values must agree"
        );

        let mut missing_role = valid;
        missing_role["role_results"]
            .as_object_mut()
            .expect("role_results is an object")
            .remove("default");
        assert!(
            serde_json::from_value::<ModelConnectionTest>(missing_role).is_err(),
            "all three role-result entries are required"
        );
    }

    #[test]
    fn save_and_verify_requires_complete_independent_role_results() {
        let settings = json!({
            "provider": "custom", "model": "default-model",
            "api_base_url": "https://models.example.test/v1", "api_key": "",
            "api_key_configured": true, "max_concurrent_model_calls": 2,
            "requests_per_minute": null, "tokens_per_minute": null,
            "analysis_model": "analysis-model", "answer_model": "answer-model",
            "default_context_capacity": 65536, "analysis_context_capacity": 32768,
            "answer_context_capacity": 65536, "default_reasoning": null,
            "analysis_reasoning": "off", "answer_reasoning": "off",
            "default_input_price_per_million": null, "default_output_price_per_million": null,
            "analysis_input_price_per_million": null, "analysis_output_price_per_million": null,
            "answer_input_price_per_million": null, "answer_output_price_per_million": null,
            "analysis_concurrency": 2,
            "provider_adapter": {
                "identity": "custom", "version": "custom.v1",
                "structured_output_mode": "json_object", "supports_structured_analysis": true,
                "supported_reasoning": ["off"], "analysis_unavailable_reason": null
            },
            "effective_roles": {
                "default": {"model": "default-model", "context_capacity": 65536, "reasoning": null, "reasoning_source": "provider_default"},
                "analysis": {"model": "analysis-model", "context_capacity": 32768, "reasoning": "off", "reasoning_source": "explicit_role"},
                "answer": {"model": "answer-model", "context_capacity": 65536, "reasoning": "off", "reasoning_source": "explicit_role"}
            },
            "analysis_capability": {"profile_identity": "analysis-profile", "status": "failed", "failure_code": "model_capability_check_failed", "reason": "Invalid structured result.", "checked_at": "2026-08-28T00:00:00Z"},
            "answer_capability": {"profile_identity": "answer-profile", "status": "verified", "failure_code": null, "reason": null, "checked_at": "2026-08-28T00:00:01Z"},
            "usage_aggregate": {"call_count": 2, "attempt_count": 2, "failure_count": 1, "input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "total_cost": null}
        });
        let result: SaveAndVerifyModelConfiguration = serde_json::from_value(json!({
            "saved": true,
            "verification_cost_accepted": true,
            "all_required_roles_verified": false,
            "cancelled": false,
            "models": ["analysis-model", "answer-model"],
            "attempt_count": 2,
            "latency_ms": 18,
            "role_results": {
                "default": {
                    "role": "default", "model": "default-model", "status": "not_required",
                    "reason": "Default is not a required runtime role.", "failure_code": null,
                    "attempt_count": 0, "profile_identity": null, "cached": false
                },
                "analysis": {
                    "role": "analysis", "model": "analysis-model", "status": "failed",
                    "reason": "Invalid structured result.", "failure_code": "model_capability_check_failed",
                    "attempt_count": 1, "profile_identity": "analysis-profile", "cached": false
                },
                "answer": {
                    "role": "answer", "model": "answer-model", "status": "verified",
                    "reason": null, "failure_code": null, "attempt_count": 1,
                    "profile_identity": "answer-profile", "cached": false
                }
            },
            "settings": settings
        }))
        .expect("complete Save and Verify response should cross the typed Bridge");

        assert!(result.saved);
        assert!(!result.all_required_roles_verified);
        assert!(matches!(
            result.role_results.analysis.status,
            ModelCapabilityCheckRoleStatus::Failed
        ));
        assert!(matches!(
            result.role_results.answer.status,
            ModelCapabilityCheckRoleStatus::Verified
        ));

        let mut missing_role =
            serde_json::to_value(&result).expect("Save and Verify result should serialize");
        missing_role["roleResults"]
            .as_object_mut()
            .expect("roleResults is an object")
            .remove("default");
        assert!(serde_json::from_value::<SaveAndVerifyModelConfiguration>(missing_role).is_err());

        let mut mismatched_role =
            serde_json::to_value(&result).expect("Save and Verify result should serialize");
        mismatched_role["roleResults"]["analysis"]["role"] = json!("answer");
        assert!(
            serde_json::from_value::<SaveAndVerifyModelConfiguration>(mismatched_role).is_err()
        );
    }
}
