//! Typed Missing Source Candidate review values for the Desktop bridge.

use super::KnowledgePageKind;
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MissingSourceCategory {
    MissingSource,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MissingSourceReason {
    SourceNotProvided,
    SourceReferenceUnresolved,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MissingSourceOutcome {
    WorkingDraft,
    Generated,
    ReviewRequired,
    Deduplicated,
    Dismissed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MissingSourceCandidate {
    #[serde(alias = "candidate_id")]
    pub candidate_id: String,
    pub category: MissingSourceCategory,
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub kind: KnowledgePageKind,
    pub title: String,
    #[serde(alias = "claim_text")]
    pub claim_text: String,
    pub reason: MissingSourceReason,
    pub section: String,
    pub locator: Value,
    #[serde(alias = "created_at")]
    pub created_at: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MissingSourceCandidatesResult {
    pub candidates: Vec<MissingSourceCandidate>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MissingSourceBindingResult {
    #[serde(alias = "candidate_id")]
    pub candidate_id: String,
    pub decision: MissingSourceDecision,
    pub outcome: MissingSourceOutcome,
    #[serde(alias = "remaining_count")]
    pub remaining_count: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MissingSourceDecision {
    Bound,
    Dismissed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MissingSourceDismissalResult {
    pub decision: MissingSourceDecision,
    #[serde(alias = "resolved_candidate_ids")]
    pub resolved_candidate_ids: Vec<String>,
    #[serde(alias = "remaining_count")]
    pub remaining_count: u32,
}

#[cfg(test)]
mod tests {
    use super::{MissingSourceCandidatesResult, MissingSourceReason};
    use serde_json::json;

    #[test]
    fn candidate_accepts_python_snake_case_fields() {
        let payload = json!({"candidates": [{
            "candidate_id": "msc-1", "category": "missing_source",
            "document_id": "doc-1", "document_name": "guide.md", "kind": "concept",
            "title": "Routing", "claim_text": "A claim", "reason": "source_not_provided",
            "section": "Setup", "locator": {"line_start": 3},
            "created_at": "2026-08-19T00:00:00+00:00"
        }]});

        let parsed: MissingSourceCandidatesResult =
            serde_json::from_value(payload).expect("candidate payload should deserialize");

        assert!(matches!(
            parsed.candidates[0].reason,
            MissingSourceReason::SourceNotProvided
        ));
    }
}
