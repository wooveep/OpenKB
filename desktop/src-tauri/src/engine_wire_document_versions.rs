//! Typed D3 document-version review values for the Desktop bridge.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum VersionMode {
    Latest,
    Exact,
    Compare,
    All,
    Unscoped,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "snake_case", deserialize = "camelCase"),
    deny_unknown_fields
)]
pub struct VersionFilter {
    pub mode: Option<VersionMode>,
    #[serde(default)]
    pub lineage_ids: Vec<String>,
    #[serde(default)]
    pub version_labels: Vec<String>,
    #[serde(default)]
    pub document_ids: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum VersionScheme {
    NumericDotted,
    Semver,
    Calendar,
    Opaque,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SnapshotKind {
    FullSnapshot,
    Delta,
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "snake_case", deserialize = "camelCase"),
    deny_unknown_fields
)]
pub struct DocumentVersionMemberDecision {
    pub document_id: String,
    pub version_label: String,
    #[serde(default = "default_branch")]
    pub branch_label: String,
    pub predecessor_document_id: Option<String>,
    #[serde(default = "default_snapshot_kind")]
    pub snapshot_kind: SnapshotKind,
    #[serde(default = "default_metadata_origin")]
    pub metadata_origin: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "snake_case", deserialize = "camelCase"),
    deny_unknown_fields
)]
pub struct ExpectedLineageRevision {
    pub lineage_id: String,
    pub metadata_revision: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "snake_case", deserialize = "camelCase"),
    deny_unknown_fields
)]
pub struct DocumentLineageDecision {
    pub display_name: String,
    pub version_scheme: VersionScheme,
    pub members: Vec<DocumentVersionMemberDecision>,
    pub current_document_id: String,
    #[serde(default)]
    pub aliases: Vec<String>,
    pub lineage_id: Option<String>,
    #[serde(default)]
    pub expected_metadata_revisions: Vec<ExpectedLineageRevision>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LineageState {
    Singleton,
    NeedsOrderReview,
    Confirmed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "camelCase", deserialize = "snake_case"),
    deny_unknown_fields
)]
pub struct DocumentVersionCatalogMember {
    pub document_id: String,
    pub document_name: String,
    pub availability: String,
    pub version_label: Option<String>,
    pub normalized_version_label: Option<String>,
    pub version_key_json: Option<String>,
    pub branch_label: Option<String>,
    pub predecessor_document_id: Option<String>,
    pub snapshot_kind: SnapshotKind,
    pub metadata_origin: Option<String>,
    pub confirmed_at: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "camelCase", deserialize = "snake_case"),
    deny_unknown_fields
)]
pub struct DocumentLineage {
    pub lineage_id: String,
    pub display_name: String,
    pub normalized_name: String,
    pub lineage_state: LineageState,
    pub version_scheme: VersionScheme,
    pub current_document_id: Option<String>,
    pub metadata_revision: u64,
    pub aliases: Vec<String>,
    pub members: Vec<DocumentVersionCatalogMember>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "camelCase", deserialize = "snake_case"),
    deny_unknown_fields
)]
pub struct DocumentVersionCatalogSnapshot {
    pub revision_id: String,
    pub source_revision: u64,
    pub snapshot_digest: String,
    pub lineages: Vec<DocumentLineage>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ContentChangeKind {
    Unchanged,
    Modified,
    Added,
    Removed,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LocationChangeKind {
    Same,
    Moved,
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "camelCase", deserialize = "snake_case"),
    deny_unknown_fields
)]
pub struct VersionDiffItem {
    pub old_block_id: Option<String>,
    pub new_block_id: Option<String>,
    pub old_evidence_id: Option<String>,
    pub new_evidence_id: Option<String>,
    pub content_change_kind: ContentChangeKind,
    pub location_change_kind: LocationChangeKind,
    pub similarity_score: f64,
    pub reason_json: String,
    pub old_locator: Option<Value>,
    pub new_locator: Option<Value>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum VersionDiffStatus {
    Ready,
    Stale,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "camelCase", deserialize = "snake_case"),
    deny_unknown_fields
)]
pub struct DocumentVersionDiff {
    pub diff_id: String,
    pub lineage_id: String,
    pub from_document_id: String,
    pub to_document_id: String,
    pub algorithm_version: String,
    pub status: VersionDiffStatus,
    pub stats: BTreeMap<String, u64>,
    pub items: Vec<VersionDiffItem>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(
    rename_all(serialize = "camelCase", deserialize = "snake_case"),
    deny_unknown_fields
)]
pub struct DocumentVersionDiffsResult {
    pub diffs: Vec<DocumentVersionDiff>,
}

fn default_branch() -> String {
    "main".to_owned()
}

fn default_snapshot_kind() -> SnapshotKind {
    SnapshotKind::FullSnapshot
}

fn default_metadata_origin() -> String {
    "user".to_owned()
}

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_accepts_python_fields_and_serializes_renderer_fields() {
        let catalog: DocumentVersionCatalogSnapshot = serde_json::from_value(serde_json::json!({
            "revision_id": "versions-1",
            "source_revision": 3,
            "snapshot_digest": "digest",
            "lineages": [{
                "lineage_id": "guide",
                "display_name": "Guide",
                "normalized_name": "guide",
                "lineage_state": "confirmed",
                "version_scheme": "numeric_dotted",
                "current_document_id": "doc-2",
                "metadata_revision": 2,
                "aliases": ["Guide"],
                "members": [{
                    "document_id": "doc-2",
                    "document_name": "Guide V2",
                    "availability": "available",
                    "version_label": "V2",
                    "normalized_version_label": "2",
                    "version_key_json": "[2]",
                    "branch_label": "main",
                    "predecessor_document_id": null,
                    "snapshot_kind": "full_snapshot",
                    "metadata_origin": "user",
                    "confirmed_at": "2026-09-05T00:00:00Z"
                }]
            }]
        }))
        .expect("valid catalog");
        let renderer = serde_json::to_value(catalog).expect("serializable catalog");
        assert_eq!(renderer["revisionId"], "versions-1");
        assert_eq!(renderer["lineages"][0]["currentDocumentId"], "doc-2");
    }

    #[test]
    fn lineage_decision_serializes_strict_engine_fields() {
        let decision: DocumentLineageDecision = serde_json::from_value(serde_json::json!({
            "displayName": "Guide",
            "versionScheme": "numeric_dotted",
            "members": [{
                "documentId": "doc-1",
                "versionLabel": "V1",
                "predecessorDocumentId": null
            }],
            "currentDocumentId": "doc-1",
            "lineageId": null
        }))
        .expect("valid renderer decision");
        let engine = serde_json::to_value(decision).expect("serializable decision");
        assert_eq!(engine["display_name"], "Guide");
        assert_eq!(engine["members"][0]["snapshot_kind"], "full_snapshot");
    }

    #[test]
    fn diff_item_exposes_both_original_source_locations() {
        let result: DocumentVersionDiffsResult = serde_json::from_value(serde_json::json!({
            "diffs": [{
                "diff_id": "diff-1",
                "lineage_id": "guide",
                "from_document_id": "doc-1",
                "to_document_id": "doc-2",
                "algorithm_version": "v1",
                "status": "ready",
                "stats": {"modified": 1},
                "items": [{
                    "old_block_id": "old-1",
                    "new_block_id": "new-1",
                    "old_evidence_id": "evidence-1",
                    "new_evidence_id": "evidence-2",
                    "content_change_kind": "modified",
                    "location_change_kind": "same",
                    "similarity_score": 0.8,
                    "reason_json": "{}",
                    "old_locator": {"page": 2},
                    "new_locator": {"page": 3}
                }]
            }]
        }))
        .expect("valid diff result");

        let renderer = serde_json::to_value(result).expect("serializable diff result");
        assert_eq!(renderer["diffs"][0]["items"][0]["oldLocator"]["page"], 2);
        assert_eq!(renderer["diffs"][0]["items"][0]["newLocator"]["page"], 3);
    }
}
