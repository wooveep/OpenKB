//! Wire-safe Knowledge Page draft, publication, and source-binding values.

use crate::engine_wire::KnowledgePageKind;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value};

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
    #[serde(alias = "lifecycle_state")]
    pub lifecycle_state: KnowledgeLifecycleState,
    #[serde(alias = "stale_after")]
    pub stale_after: Option<String>,
    #[serde(alias = "is_stale")]
    pub is_stale: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgePagePublicationState {
    Draft,
    UnpublishedChanges,
    Published,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeLifecycleState {
    Draft,
    Stable,
    Deprecated,
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
#[serde(rename_all = "snake_case")]
pub enum KnowledgeVerificationState {
    Unverified,
    HumanReviewed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeVerificationReason {
    PublishRequired,
    WorkingDraftNotVerifiable,
    NotVerified,
    RevisionChanged,
    PublicationGateBlocked,
    LegacyUnmappedNotVerifiable,
    DeprecatedNotVerifiable,
    LifecycleChanged,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeVerificationStatus {
    pub state: KnowledgeVerificationState,
    #[serde(alias = "can_verify")]
    pub can_verify: bool,
    pub reason: Option<KnowledgeVerificationReason>,
    pub actor: Option<String>,
    #[serde(alias = "verified_at")]
    pub verified_at: Option<String>,
    #[serde(alias = "revision_id")]
    pub revision_id: Option<String>,
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
    #[serde(alias = "lifecycle_state")]
    pub lifecycle_state: KnowledgeLifecycleState,
    #[serde(alias = "stale_after")]
    pub stale_after: Option<String>,
    #[serde(alias = "is_stale")]
    pub is_stale: bool,
    #[serde(alias = "published_revision")]
    pub published_revision: Option<KnowledgePublishedRevision>,
    #[serde(alias = "working_draft")]
    pub working_draft: Option<KnowledgeWorkingDraft>,
    pub verification: KnowledgeVerificationStatus,
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
#[serde(tag = "authority", rename_all = "snake_case")]
pub enum KnowledgeWorkspaceItemRequest {
    Generated(KnowledgeGeneratedItemRequest),
    User(KnowledgeUserItemRequest),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeGeneratedItemRequest {
    #[serde(alias = "generation_id")]
    pub generation_id: u64,
    #[serde(alias = "item_key")]
    pub item_key: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeUserItemRequest {
    #[serde(alias = "page_id")]
    pub page_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "authority", rename_all = "snake_case")]
pub enum KnowledgeWorkspaceItemSummary {
    Generated(KnowledgeGeneratedSummary),
    User(KnowledgeUserSummary),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeGeneratedSummary {
    pub identity: String,
    pub kind: KnowledgePageKind,
    pub title: String,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
    pub current: bool,
    #[serde(alias = "generation_id")]
    pub generation_id: u64,
    #[serde(alias = "item_key")]
    pub item_key: String,
    #[serde(alias = "provenance_state")]
    pub provenance_state: KnowledgeProvenanceState,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeUserSummary {
    pub identity: String,
    pub kind: KnowledgePageKind,
    pub title: String,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
    pub current: bool,
    #[serde(alias = "page_id")]
    pub page_id: String,
    #[serde(alias = "publication_state")]
    pub publication_state: KnowledgePagePublicationState,
    #[serde(alias = "lifecycle_state")]
    pub lifecycle_state: KnowledgeLifecycleState,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeWorkspaceResult {
    #[serde(
        alias = "current_generation_id",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub current_generation_id: Option<u64>,
    pub items: Vec<KnowledgeWorkspaceItemSummary>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "authority", rename_all = "snake_case")]
pub enum KnowledgeWorkspaceItemDetail {
    Generated(KnowledgeGeneratedDetail),
    User(KnowledgeUserDetail),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeGeneratedDetail {
    pub identity: String,
    pub editable: RequiredBool<false>,
    pub kind: KnowledgePageKind,
    pub title: String,
    #[serde(alias = "generation_id")]
    pub generation_id: u64,
    #[serde(alias = "item_key")]
    pub item_key: String,
    #[serde(alias = "content_markdown")]
    pub content_markdown: String,
    #[serde(
        alias = "entity_subtype",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub entity_subtype: Option<String>,
    pub aliases: Vec<String>,
    pub tags: Vec<String>,
    #[serde(alias = "created_at")]
    pub created_at: String,
    #[serde(alias = "provenance_state")]
    pub provenance_state: KnowledgeProvenanceState,
    #[serde(alias = "analysis_provenance")]
    pub analysis_provenance: Map<String, Value>,
    #[serde(alias = "source_map")]
    pub source_map: Vec<KnowledgeSourceMapEntry>,
    pub current: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeUserDetail {
    pub identity: String,
    pub editable: RequiredBool<true>,
    #[serde(alias = "page_id")]
    pub page_id: String,
    pub kind: KnowledgePageKind,
    pub title: String,
    #[serde(alias = "publication_state")]
    pub publication_state: KnowledgePagePublicationState,
    #[serde(
        alias = "published_revision_number",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub published_revision_number: Option<u32>,
    #[serde(alias = "materialized_path")]
    pub materialized_path: String,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
    #[serde(alias = "lifecycle_state")]
    pub lifecycle_state: KnowledgeLifecycleState,
    #[serde(
        alias = "stale_after",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub stale_after: Option<String>,
    #[serde(alias = "is_stale")]
    pub is_stale: bool,
    #[serde(
        alias = "published_revision",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub published_revision: Option<KnowledgePublishedRevision>,
    #[serde(
        alias = "working_draft",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub working_draft: Option<KnowledgeWorkingDraft>,
    pub verification: KnowledgeVerificationStatus,
    #[serde(default, alias = "publication_diagnostics")]
    pub publication_diagnostics: Vec<KnowledgePublicationDiagnostic>,
}

#[derive(Clone, Debug, Default)]
pub struct RequiredBool<const EXPECTED: bool>;

impl<const EXPECTED: bool> Serialize for RequiredBool<EXPECTED> {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_bool(EXPECTED)
    }
}

impl<'de, const EXPECTED: bool> Deserialize<'de> for RequiredBool<EXPECTED> {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = bool::deserialize(deserializer)?;
        if value != EXPECTED {
            return Err(<D::Error as serde::de::Error>::custom(format!(
                "editable must be {EXPECTED} for this authority"
            )));
        }
        Ok(Self)
    }
}

fn deserialize_required_nullable<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeGenerationSummary {
    #[serde(alias = "generation_id")]
    pub generation_id: u64,
    #[serde(
        alias = "parent_generation_id",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub parent_generation_id: Option<u64>,
    #[serde(alias = "created_at")]
    pub created_at: String,
    #[serde(alias = "item_count")]
    pub item_count: u64,
    pub current: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum KnowledgeWorkspaceHistory {
    Index(KnowledgeWorkspaceHistoryIndex),
    Generation(KnowledgeWorkspaceGenerationHistory),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeWorkspaceHistoryIndex {
    #[serde(
        alias = "current_generation_id",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub current_generation_id: Option<u64>,
    pub generations: Vec<KnowledgeGenerationSummary>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeWorkspaceGenerationHistory {
    #[serde(alias = "generation_id")]
    pub generation_id: u64,
    #[serde(
        alias = "parent_generation_id",
        deserialize_with = "deserialize_required_nullable"
    )]
    pub parent_generation_id: Option<u64>,
    #[serde(alias = "created_at")]
    pub created_at: String,
    pub current: bool,
    pub items: Vec<KnowledgeWorkspaceItemSummary>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeAdoptionMatch {
    Exact,
    Possible,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeAdoptionCandidate {
    #[serde(alias = "page_id")]
    pub page_id: String,
    pub title: String,
    #[serde(alias = "publication_state")]
    pub publication_state: KnowledgePagePublicationState,
    #[serde(rename = "match")]
    pub match_kind: KnowledgeAdoptionMatch,
    pub confidence: f64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeAdoptionDecision {
    CreateNew,
    UseExisting,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeAdoptionStatus {
    Adopted,
    AlreadyAdopted,
    ReconciliationRequired,
    ChoiceRequired,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeOriginReference {
    #[serde(alias = "generation_id")]
    pub generation_id: u64,
    #[serde(alias = "item_key")]
    pub item_key: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KnowledgeAdoptionResult {
    pub status: KnowledgeAdoptionStatus,
    #[serde(alias = "generation_id")]
    pub generation_id: u64,
    #[serde(alias = "item_key")]
    pub item_key: String,
    #[serde(alias = "page_id", deserialize_with = "deserialize_required_nullable")]
    pub page_id: Option<String>,
    pub origin: KnowledgeOriginReference,
    pub candidates: Vec<KnowledgeAdoptionCandidate>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeSourcesResult {
    pub sources: Vec<KnowledgeSourceCandidate>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgePageDeletionResult {
    #[serde(alias = "page_id")]
    pub page_id: String,
    pub deleted: bool,
}

#[cfg(test)]
mod tests {
    use super::{
        KnowledgeAdoptionMatch, KnowledgeAdoptionResult, KnowledgeVerificationState,
        KnowledgeVerificationStatus, KnowledgeWorkspaceHistory, KnowledgeWorkspaceItemDetail,
        KnowledgeWorkspaceItemRequest, KnowledgeWorkspaceItemSummary, KnowledgeWorkspaceResult,
    };
    use serde_json::json;

    #[test]
    fn verification_status_accepts_python_snake_case_fields() {
        let status: KnowledgeVerificationStatus = serde_json::from_value(json!({
            "state": "human_reviewed",
            "can_verify": false,
            "reason": null,
            "actor": "local_user",
            "verified_at": "2026-08-19T10:00:00Z",
            "revision_id": "revision-1"
        }))
        .expect("Python verification payload should deserialize");

        assert!(matches!(
            status.state,
            KnowledgeVerificationState::HumanReviewed
        ));
        assert!(!status.can_verify);
        assert_eq!(status.actor.as_deref(), Some("local_user"));
        assert_eq!(status.verified_at.as_deref(), Some("2026-08-19T10:00:00Z"));
        assert_eq!(status.revision_id.as_deref(), Some("revision-1"));
    }

    #[test]
    fn workspace_accepts_generated_and_user_authorities_without_merging_them() {
        let workspace: KnowledgeWorkspaceResult = serde_json::from_value(json!({
            "current_generation_id": 7,
            "items": [
                {
                    "authority": "generated", "identity": "generated:7:item-1",
                    "kind": "concept", "title": "Generated", "updated_at": "2026-08-28T00:00:00Z",
                    "current": true, "generation_id": 7, "item_key": "item-1",
                    "provenance_state": "source_backed"
                },
                {
                    "authority": "user", "identity": "user:page-1", "kind": "entity",
                    "title": "User page", "updated_at": "2026-08-28T00:00:01Z",
                    "current": true,
                    "page_id": "page-1", "publication_state": "draft",
                    "lifecycle_state": "draft"
                }
            ]
        }))
        .expect("workspace authority summaries should deserialize");
        assert_eq!(workspace.items.len(), 2);
        assert!(matches!(
            &workspace.items[0],
            KnowledgeWorkspaceItemSummary::Generated(_)
        ));
        assert!(matches!(
            &workspace.items[1],
            KnowledgeWorkspaceItemSummary::User(_)
        ));
        assert!(serde_json::from_value::<KnowledgeWorkspaceResult>(json!({
            "items": []
        }))
        .is_err());

        let generated_detail = json!({
            "authority": "generated", "identity": "generated:7:item-1", "editable": false,
            "generation_id": 7, "item_key": "item-1", "kind": "concept", "title": "Generated",
            "content_markdown": "# Generated", "entity_subtype": null, "aliases": [], "tags": [],
            "created_at": "2026-08-28T00:00:00Z", "provenance_state": "source_backed",
            "analysis_provenance": {}, "source_map": [], "current": true
        });
        let detail: KnowledgeWorkspaceItemDetail = serde_json::from_value(generated_detail.clone())
            .expect("generated detail should deserialize as a read-only item");
        let KnowledgeWorkspaceItemDetail::Generated(detail) = detail else {
            panic!("generated authority must produce a generated detail")
        };
        assert_eq!(detail.generation_id, 7);
        assert!(detail.analysis_provenance.is_empty());
        for required_field in [
            "entity_subtype",
            "aliases",
            "tags",
            "analysis_provenance",
            "source_map",
        ] {
            let mut missing = generated_detail.clone();
            missing
                .as_object_mut()
                .expect("generated detail fixture should be an object")
                .remove(required_field);
            assert!(
                serde_json::from_value::<KnowledgeWorkspaceItemDetail>(missing).is_err(),
                "missing {required_field} must fail closed"
            );
        }

        assert!(serde_json::from_value::<KnowledgeWorkspaceItemDetail>(json!({
            "authority": "generated", "identity": "generated:7:item-1", "editable": true,
            "generation_id": 7, "item_key": "item-1", "kind": "concept", "title": "Generated",
            "content_markdown": "# Generated", "entity_subtype": null, "aliases": [], "tags": [],
            "created_at": "2026-08-28T00:00:00Z", "provenance_state": "source_backed",
            "analysis_provenance": {}, "source_map": [], "current": true
        })).is_err());
        assert!(serde_json::from_value::<KnowledgeWorkspaceItemDetail>(json!({
            "authority": "generated", "identity": "generated:7:item-1", "editable": false,
            "generation_id": 7, "item_key": "item-1", "kind": "concept", "title": "Generated",
            "content_markdown": "# Generated", "entity_subtype": null, "aliases": [], "tags": [],
            "created_at": "2026-08-28T00:00:00Z", "provenance_state": "source_backed",
            "analysis_provenance": [], "source_map": [], "current": true
        })).is_err());
    }

    #[test]
    fn workspace_history_requires_nullable_and_collection_fields() {
        let index = json!({
            "current_generation_id": null,
            "generations": [{
                "generation_id": 7,
                "parent_generation_id": null,
                "created_at": "2026-08-28T00:00:00Z",
                "item_count": 1,
                "current": true
            }]
        });
        serde_json::from_value::<KnowledgeWorkspaceHistory>(index.clone())
            .expect("complete history index should deserialize");
        for required_field in ["current_generation_id", "generations"] {
            let mut missing = index.clone();
            missing
                .as_object_mut()
                .expect("history fixture should be an object")
                .remove(required_field);
            assert!(serde_json::from_value::<KnowledgeWorkspaceHistory>(missing).is_err());
        }
        let generation = json!({
            "generation_id": 7,
            "parent_generation_id": null,
            "created_at": "2026-08-28T00:00:00Z",
            "current": true,
            "items": []
        });
        serde_json::from_value::<KnowledgeWorkspaceHistory>(generation.clone())
            .expect("complete generation history should deserialize");
        for required_field in ["parent_generation_id", "items"] {
            let mut missing = generation.clone();
            missing
                .as_object_mut()
                .expect("generation fixture should be an object")
                .remove(required_field);
            assert!(serde_json::from_value::<KnowledgeWorkspaceHistory>(missing).is_err());
        }
        let mut missing_parent = index;
        missing_parent["generations"][0]
            .as_object_mut()
            .expect("generation summary should be an object")
            .remove("parent_generation_id");
        assert!(serde_json::from_value::<KnowledgeWorkspaceHistory>(missing_parent).is_err());
    }

    #[test]
    fn adoption_candidate_match_is_a_closed_wire_enum() {
        let adoption = json!({
            "status": "choice_required",
            "generation_id": 7,
            "item_key": "item-1",
            "page_id": null,
            "origin": { "generation_id": 7, "item_key": "item-1" },
            "candidates": [{
                "page_id": "page-1",
                "title": "Existing",
                "publication_state": "draft",
                "match": "exact",
                "confidence": 1.0
            }]
        });
        let result: KnowledgeAdoptionResult = serde_json::from_value(adoption.clone())
            .expect("exact adoption match should deserialize");
        assert!(matches!(
            result.candidates[0].match_kind,
            KnowledgeAdoptionMatch::Exact
        ));
        for required_field in ["page_id", "candidates"] {
            let mut missing = adoption.clone();
            missing
                .as_object_mut()
                .expect("adoption fixture should be an object")
                .remove(required_field);
            assert!(serde_json::from_value::<KnowledgeAdoptionResult>(missing).is_err());
        }
        assert!(serde_json::from_value::<KnowledgeAdoptionResult>(json!({
            "status": "choice_required",
            "generation_id": 7,
            "item_key": "item-1",
            "page_id": null,
            "origin": { "generation_id": 7, "item_key": "item-1" },
            "candidates": [{
                "page_id": "page-1", "title": "Existing",
                "publication_state": "draft", "match": "unknown", "confidence": 0.5
            }]
        }))
        .is_err());
    }

    #[test]
    fn workspace_item_request_rejects_missing_and_hybrid_authority_fields() {
        let generated: KnowledgeWorkspaceItemRequest = serde_json::from_value(json!({
            "authority": "generated", "generationId": 7, "itemKey": "item-1"
        }))
        .expect("generated request should deserialize");
        assert!(matches!(
            generated,
            KnowledgeWorkspaceItemRequest::Generated(_)
        ));
        let user: KnowledgeWorkspaceItemRequest = serde_json::from_value(json!({
            "authority": "user", "pageId": "page-1"
        }))
        .expect("user request should deserialize");
        assert!(matches!(user, KnowledgeWorkspaceItemRequest::User(_)));

        assert!(
            serde_json::from_value::<KnowledgeWorkspaceItemRequest>(json!({
                "authority": "generated", "generationId": 7, "itemKey": "item-1",
                "pageId": "page-1"
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<KnowledgeWorkspaceItemRequest>(json!({
                "authority": "generated", "generationId": 7
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<KnowledgeWorkspaceItemRequest>(json!({
                "authority": "user", "pageId": "page-1", "itemKey": "item-1"
            }))
            .is_err()
        );
    }
}
