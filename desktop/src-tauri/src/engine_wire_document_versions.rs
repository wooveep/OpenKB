//! Typed D3 document-version review values for the Desktop bridge.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DocumentVersionCandidateDecision {
    LinkToCandidate,
    KeepSeparate,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DocumentVersionCandidateStatus {
    Pending,
    Accepted,
    Rejected,
    Dismissed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DocumentVersionCandidate {
    #[serde(alias = "candidate_id")]
    pub candidate_id: String,
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    #[serde(alias = "candidate_document_id")]
    pub candidate_document_id: String,
    #[serde(alias = "candidate_document_name")]
    pub candidate_document_name: String,
    #[serde(alias = "lexical_score")]
    pub lexical_score: f64,
    #[serde(alias = "character_score")]
    pub character_score: f64,
    pub reason: String,
    pub status: DocumentVersionCandidateStatus,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DocumentVersionCandidatesResult {
    pub candidates: Vec<DocumentVersionCandidate>,
}
