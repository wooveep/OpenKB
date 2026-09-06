//! Snapshot-bound semantic review decisions and source excerpts.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticReviewDecision {
    Compatible,
    SameIdentity,
    KeepSeparate,
    KeepCurrent,
    Conflicting,
    Unresolved,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SemanticReviews {
    pub items: Vec<SemanticReview>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SemanticReview {
    #[serde(alias = "review_id")]
    pub review_id: String,
    pub reason: String,
    pub status: String,
    pub decision: Option<SemanticReviewDecision>,
    pub authority: Option<String>,
    pub choices: Vec<SemanticReviewDecision>,
    pub candidates: Vec<SemanticCandidate>,
    pub evidence: Vec<SemanticEvidence>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SemanticCandidate {
    #[serde(alias = "candidate_id")]
    pub candidate_id: String,
    #[serde(alias = "candidate_generation_id")]
    pub candidate_generation_id: String,
    #[serde(alias = "document_id")]
    pub document_id: String,
    pub title: String,
    pub kind: String,
    pub aliases: Vec<String>,
    pub claims: Vec<SemanticClaim>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SemanticClaim {
    pub text: String,
    pub applicability: Vec<(String, String)>,
    #[serde(alias = "evidence_ids")]
    pub evidence_ids: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SemanticEvidence {
    #[serde(alias = "evidence_id")]
    pub evidence_id: String,
    pub text: String,
}

#[cfg(test)]
mod tests {
    use super::{SemanticReviewDecision, SemanticReviews};
    use serde_json::json;

    #[test]
    fn review_snapshot_converts_nested_python_fields_for_the_renderer() {
        let value: SemanticReviews = serde_json::from_value(json!({"items": [{
            "review_id": "review-1", "reason": "claim_relationship_review", "status": "pending",
            "decision": "conflicting", "authority": "model", "choices": ["compatible", "keep_current"],
            "candidates": [{"candidate_id": "candidate-1", "candidate_generation_id": "generation-1",
                "document_id": "document-1", "title": "Example", "kind": "entity", "aliases": [],
                "claims": [{"text": "A claim.", "applicability": [["version", "1"]], "evidence_ids": ["source-1"]}]}],
            "evidence": [{"evidence_id": "source-1", "text": "An original excerpt."}]
        }]})).unwrap();
        let encoded = serde_json::to_value(value).unwrap();
        assert_eq!(encoded["items"][0]["reviewId"], "review-1");
        assert_eq!(
            encoded["items"][0]["candidates"][0]["claims"][0]["evidenceIds"][0],
            "source-1"
        );
        assert_eq!(encoded["items"][0]["evidence"][0]["evidenceId"], "source-1");
    }

    #[test]
    fn unsupported_decisions_fail_at_the_native_boundary() {
        assert!(serde_json::from_value::<SemanticReviewDecision>(json!("force_publish")).is_err());
    }
}
