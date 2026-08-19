//! Wire-safe Knowledge Page draft, publication, and source-binding values.

use crate::engine_wire::KnowledgePageKind;
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgePageSummary {
    #[serde(alias = "page_id")]
    pub page_id: String,
    pub kind: KnowledgePageKind,
    pub title: String,
    #[serde(alias = "publication_state")]
    pub publication_state: KnowledgePagePublicationState,
    #[serde(alias = "published_revision_number")]
    pub published_revision_number: Option<u32>,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgePagePublicationState {
    Draft,
    UnpublishedChanges,
    Published,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeSourceCandidate {
    #[serde(alias = "evidence_id")]
    pub evidence_id: String,
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub section: String,
    pub locator: Value,
    pub excerpt: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeSourceMapEntry {
    #[serde(alias = "source_id")]
    pub source_id: String,
    #[serde(alias = "claim_text")]
    pub claim_text: String,
    pub availability: KnowledgeSourceAvailability,
    #[serde(flatten)]
    pub evidence: KnowledgeSourceCandidate,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeSourceAvailability {
    Available,
    Unavailable,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeProvenanceState {
    SourceBacked,
    Structural,
    LegacyUnmapped,
    Unsourced,
    Invalid,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgePublicationDiagnostic {
    pub code: String,
    pub message: String,
    #[serde(alias = "source_id")]
    pub source_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgePublishedRevision {
    #[serde(alias = "revision_number")]
    pub revision_number: u32,
    pub title: String,
    #[serde(alias = "content_markdown")]
    pub content_markdown: String,
    #[serde(alias = "published_at")]
    pub published_at: String,
    #[serde(alias = "provenance_state")]
    pub provenance_state: KnowledgeProvenanceState,
    #[serde(default, alias = "source_map")]
    pub source_map: Vec<KnowledgeSourceMapEntry>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeWorkingDraft {
    pub title: String,
    #[serde(alias = "content_markdown")]
    pub content_markdown: String,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
    #[serde(alias = "provenance_state")]
    pub provenance_state: KnowledgeProvenanceState,
    #[serde(default, alias = "source_map")]
    pub source_map: Vec<KnowledgeSourceMapEntry>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgePage {
    #[serde(alias = "page_id")]
    pub page_id: String,
    pub kind: KnowledgePageKind,
    pub title: String,
    #[serde(alias = "publication_state")]
    pub publication_state: KnowledgePagePublicationState,
    #[serde(alias = "published_revision_number")]
    pub published_revision_number: Option<u32>,
    #[serde(alias = "materialized_path")]
    pub materialized_path: String,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
    #[serde(alias = "published_revision")]
    pub published_revision: Option<KnowledgePublishedRevision>,
    #[serde(alias = "working_draft")]
    pub working_draft: Option<KnowledgeWorkingDraft>,
    #[serde(default, alias = "publication_diagnostics")]
    pub publication_diagnostics: Vec<KnowledgePublicationDiagnostic>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgePagesResult {
    pub pages: Vec<KnowledgePageSummary>,
    #[serde(alias = "selected_page_id")]
    pub selected_page_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeSourcesResult {
    pub sources: Vec<KnowledgeSourceCandidate>,
}
