//! Grounded-answer request methods for the private Engine transport.

use super::{BridgeError, BridgeResult, EngineSupervisor, GroundedAnswer, VersionFilter};
use serde_json::json;

impl EngineSupervisor {
    pub fn ask_grounded(
        &self,
        question: String,
        version_filter: Option<VersionFilter>,
        request_id: String,
    ) -> BridgeResult<GroundedAnswer> {
        self.request_grounded_answer(
            "workbench.ask_grounded",
            json!({ "question": question, "version_filter": version_filter }),
            request_id,
            "Engine grounded answer response has an invalid shape",
        )
    }

    pub fn retry_interrupted_answer(
        &self,
        answer_id: String,
        request_id: String,
    ) -> BridgeResult<GroundedAnswer> {
        self.request_grounded_answer(
            "workbench.retry_interrupted_answer",
            json!({ "answer_id": answer_id }),
            request_id,
            "Engine interrupted-answer retry response has an invalid shape",
        )
    }

    fn request_grounded_answer(
        &self,
        method: &str,
        params: serde_json::Value,
        request_id: String,
        invalid_shape_message: &str,
    ) -> BridgeResult<GroundedAnswer> {
        self.ensure_started()?;
        let value = self.request_started_with_wait(method, params, Some(request_id), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("{invalid_shape_message}: {error}"),
            )
        })
    }
}
