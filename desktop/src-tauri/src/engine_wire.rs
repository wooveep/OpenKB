//! Typed wire values and framing for the private Desktop Shell ↔ Engine bridge.

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::{Read, Write};

#[path = "engine_wire_catalog.rs"]
mod catalog;
pub use catalog::CatalogRebuildTask;

#[path = "engine_wire_document_versions.rs"]
mod document_versions;
pub use document_versions::{
    DocumentVersionCandidate, DocumentVersionCandidateDecision, DocumentVersionCandidatesResult,
};
#[path = "engine_wire_knowledge_reconciliation.rs"]
mod knowledge_reconciliation;
pub use knowledge_reconciliation::{
    KnowledgeReconciliationCommit, KnowledgeReconciliationConflictsResult,
    KnowledgeReconciliationDecision,
};
#[path = "engine_wire_knowledge_reanalysis.rs"]
mod knowledge_reanalysis;
pub use knowledge_reanalysis::{KnowledgeReanalysisOverview, KnowledgeReanalysisRun};
#[path = "engine_wire_knowledge_graph.rs"]
mod knowledge_graph;
pub use knowledge_graph::{KnowledgeGraphExtractionControlResult, KnowledgeGraphExtractionTask};
#[path = "engine_wire_missing_sources.rs"]
mod missing_sources;
pub use missing_sources::{
    MissingSourceBindingResult, MissingSourceCandidatesResult, MissingSourceDismissalResult,
};
#[path = "engine_wire_model_lifecycle.rs"]
mod model_lifecycle;
pub use model_lifecycle::{ModelCallLifecycleEventData, ModelCallLifecycleStatus};
#[path = "engine_wire_import_observability.rs"]
mod import_observability;
pub use import_observability::{
    ImportProgressStep, ImportUsageAggregate, LegacyModelRecovery, ModelActivity, ModelUsageRecord,
};
#[path = "engine_wire_page_tree.rs"]
mod page_tree;
pub use page_tree::{PageTreeEnrichmentControlResult, PageTreeEnrichmentTask, PageTreeRebuildTask};
#[path = "engine_wire_retrieval.rs"]
mod retrieval;
pub use retrieval::{GroundedAnswer, GroundedAnswersResult, RetrievalTrace};
#[path = "engine_wire_settings.rs"]
mod settings;
pub use settings::{
    DiagnosticBundleResult, ModelSettings, ModelSettingsDraft, ModelUsageAggregate,
};

#[cfg(test)]
#[path = "engine_wire_import_tests.rs"]
mod import_wire_tests;
#[cfg(test)]
#[path = "engine_wire_tests.rs"]
mod tests;

pub(crate) const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;

pub type BridgeResult<T> = Result<T, BridgeError>;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BridgeError {
    pub code: String,
    pub message: String,
}

impl BridgeError {
    pub(crate) fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BridgeHandshake {
    pub protocol_version: u32,
    pub engine_version: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngineHealth {
    pub status: EngineHealthStatus,
    pub protocol_version: u32,
    pub parser_readiness: BTreeMap<String, ParserReadiness>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ParserReadiness {
    pub family: ParserFamily,
    pub formats: Vec<String>,
    pub resource_state: ParserResourceState,
    pub runtime_state: ParserRuntimeState,
    pub diagnostic: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ParserFamily {
    Text,
    NativeOffice,
    LegacyOffice,
    Pdf,
    PdfOcr,
    DeepDocument,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ParserRoute {
    Auto,
    PlainText,
    DirectStructured,
    PymupdfFast,
    BundledOnnxOcr,
    TikaLegacy,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ParserResourceState {
    ResourcesReady,
    Unavailable,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ParserRuntimeState {
    NotLoaded,
    Initializing,
    Ready,
    Unavailable,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum EngineHealthStatus {
    Ready,
    Starting,
    Unavailable,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImportStage {
    Preflight,
    RawAsset,
    DocumentIr,
    Evidence,
    DeterministicPageTree,
    ModelAnalysis,
    Search,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ImportStageStatus {
    Pending,
    Running,
    Paused,
    Cancelled,
    Completed,
    Failed,
    Skipped,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImportJobStatus {
    Running,
    Paused,
    AwaitingModelConfiguration,
    Cancelled,
    Recoverable,
    Quarantined,
    Completed,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelCallStatus {
    Running,
    RetryWait,
    Completed,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum KnowledgePageKind {
    Concept,
    Entity,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ImportedDocumentAvailability {
    Available,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ImportSourceStatus {
    Supported,
    Unsupported,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ImportedDocumentSourceFormat {
    Txt,
    Markdown,
    Doc,
    Docx,
    Xls,
    Xlsx,
    Ppt,
    Pptx,
    Pdf,
}

#[derive(Clone, Debug, Serialize)]
#[serde(transparent)]
pub struct ImportProgress(u8);

impl<'de> Deserialize<'de> for ImportProgress {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = u8::deserialize(deserializer)?;
        if value > 100 {
            return Err(serde::de::Error::custom(
                "import progress must be between 0 and 100",
            ));
        }
        Ok(Self(value))
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BridgeEvent {
    pub sequence: u64,
    #[serde(flatten)]
    pub event: EngineEvent,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", content = "data")]
pub enum EngineEvent {
    #[serde(rename = "engine.request_started")]
    RequestStarted(EngineRequestEventData),
    #[serde(rename = "engine.request_cancelled")]
    RequestCancelled(EngineRequestEventData),
    #[serde(rename = "engine.request_completed")]
    RequestCompleted(EngineRequestEventData),
    #[serde(rename = "import.stage_progress")]
    ImportStageProgress(ImportStageProgressEventData),
    #[serde(rename = "answer.delta")]
    AnswerDelta(AnswerDeltaEventData),
    #[serde(rename = "knowledge_reanalysis.updated")]
    KnowledgeReanalysisUpdated(KnowledgeReanalysisUpdatedEventData),
    #[serde(rename = "model.call_lifecycle")]
    ModelCallLifecycle(ModelCallLifecycleEventData),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngineRequestEventData {
    #[serde(alias = "request_id")]
    pub request_id: String,
    pub ok: Option<bool>,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeReanalysisUpdatedEventData {
    #[serde(alias = "run_id")]
    pub run_id: String,
    #[serde(alias = "job_id")]
    pub job_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportStageProgressEventData {
    #[serde(alias = "request_id")]
    pub request_id: Option<String>,
    #[serde(alias = "job_id")]
    pub job_id: String,
    #[serde(alias = "stage_run_id")]
    pub stage_run_id: String,
    pub stage: ImportStage,
    pub status: ImportStageStatus,
    pub progress: ImportProgress,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
    #[serde(alias = "document_id")]
    pub document_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AnswerDeltaEventData {
    #[serde(alias = "request_id")]
    pub request_id: String,
    #[serde(alias = "answer_id")]
    pub answer_id: String,
    pub delta: String,
    #[serde(default)]
    pub replace: bool,
    #[serde(default = "default_answer_attempt")]
    pub attempt: u32,
}

fn default_answer_attempt() -> u32 {
    1
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CancelResult {
    pub cancelled: bool,
    pub request_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopKnowledgeBase {
    #[serde(alias = "kb_dir")]
    pub kb_dir: String,
    pub name: String,
    #[serde(alias = "schema_version")]
    pub schema_version: u32,
    #[serde(alias = "last_checkpoint_at")]
    pub last_checkpoint_at: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ActiveKnowledgeBaseResult {
    #[serde(alias = "knowledge_base")]
    pub knowledge_base: Option<DesktopKnowledgeBase>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeBaseActivationResult {
    #[serde(alias = "knowledge_base")]
    pub knowledge_base: DesktopKnowledgeBase,
    pub events: Vec<KnowledgeBaseEvent>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportedDocument {
    #[serde(alias = "document_id")]
    pub document_id: String,
    pub name: String,
    #[serde(alias = "source_format")]
    pub source_format: ImportedDocumentSourceFormat,
    #[serde(alias = "raw_asset_sha256")]
    pub raw_asset_sha256: String,
    #[serde(alias = "evidence_count")]
    pub evidence_count: u64,
    pub availability: ImportedDocumentAvailability,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RawDocument {
    #[serde(alias = "document_id")]
    pub document_id: String,
    pub name: String,
    #[serde(alias = "source_format")]
    pub source_format: ImportedDocumentSourceFormat,
    #[serde(alias = "asset_sha256")]
    pub asset_sha256: String,
    #[serde(alias = "byte_size")]
    pub byte_size: u64,
    pub content: String,
    pub page: u32,
    #[serde(alias = "has_more")]
    pub has_more: bool,
    #[serde(default, alias = "source_images")]
    pub source_images: Vec<SourceImage>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SourceImage {
    #[serde(alias = "source_image_id")]
    pub source_image_id: String,
    pub name: String,
    #[serde(alias = "media_type")]
    pub media_type: String,
    #[serde(alias = "file_path")]
    pub file_path: String,
    #[serde(alias = "alt_text")]
    pub alt_text: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportJob {
    #[serde(alias = "job_id")]
    pub job_id: String,
    #[serde(alias = "source_name")]
    pub source_name: String,
    pub status: ImportJobStatus,
    pub progress: ImportProgress,
    #[serde(alias = "document_id")]
    pub document_id: Option<String>,
    pub deduplicated: bool,
    #[serde(default)]
    pub deduplication: Option<ImportDeduplication>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportDeduplication {
    pub level: String,
    pub reason: String,
    #[serde(alias = "reused_document_id")]
    pub reused_document_id: Option<String>,
    #[serde(alias = "reused_evidence_count")]
    pub reused_evidence_count: u32,
    #[serde(default, alias = "reusable_stages")]
    pub reusable_stages: Vec<ImportStage>,
    #[serde(alias = "normalized_body_sha256")]
    pub normalized_body_sha256: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportSource {
    pub path: String,
    pub name: String,
    pub status: ImportSourceStatus,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportSourceInspection {
    pub supported: Vec<ImportSource>,
    pub unsupported: Vec<ImportSource>,
    #[serde(alias = "supported_extensions")]
    pub supported_extensions: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportStageRun {
    #[serde(alias = "stage_run_id")]
    pub stage_run_id: String,
    pub stage: ImportStage,
    pub status: ImportStageStatus,
    pub progress: ImportProgress,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelAttempt {
    pub attempt: u32,
    pub status: ModelCallStatus,
    #[serde(default, alias = "elapsed_seconds")]
    pub elapsed_seconds: f64,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelCall {
    #[serde(alias = "call_id")]
    pub call_id: String,
    #[serde(alias = "stage_run_id")]
    pub stage_run_id: String,
    pub operation: String,
    pub status: ModelCallStatus,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u32,
    #[serde(default, alias = "elapsed_seconds")]
    pub elapsed_seconds: f64,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
    pub reason: Option<String>,
    #[serde(alias = "suggested_action")]
    pub suggested_action: Option<String>,
    #[serde(default)]
    pub attempts: Vec<ModelAttempt>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct QuarantinedDocument {
    #[serde(alias = "stage_run_id")]
    pub stage_run_id: String,
    pub stage: ImportStage,
    #[serde(alias = "error_code")]
    pub error_code: String,
    pub reason: String,
    #[serde(alias = "suggested_action")]
    pub suggested_action: String,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeAnalysisPhase {
    Batches,
    Merge,
    Completed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeAnalysisProgress {
    pub total: u32,
    pub completed: u32,
    pub active: u32,
    pub failed: u32,
    #[serde(alias = "current_batch")]
    pub current_batch: Option<u32>,
    pub phase: KnowledgeAnalysisPhase,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RecoveryOverride {
    pub model: Option<String>,
    #[serde(default, alias = "context_capacity")]
    pub context_capacity: Option<u64>,
    #[serde(default, alias = "legacy_recovery_choice")]
    pub legacy_recovery_choice: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TextDocumentImportResult {
    pub document: ImportedDocument,
    pub job: ImportJob,
    pub stages: Vec<ImportStageRun>,
    #[serde(default, alias = "model_calls")]
    pub model_calls: Vec<ModelCall>,
    pub quarantine: Option<QuarantinedDocument>,
    #[serde(default, alias = "knowledge_analysis")]
    pub knowledge_analysis: Option<KnowledgeAnalysisProgress>,
    #[serde(default, alias = "import_progress")]
    pub import_progress: Vec<ImportProgressStep>,
    #[serde(default, alias = "model_usage")]
    pub model_usage: Vec<ModelUsageRecord>,
    #[serde(default, alias = "model_usage_aggregate")]
    pub model_usage_aggregate: Option<ImportUsageAggregate>,
    #[serde(default, alias = "model_activity")]
    pub model_activity: Option<ModelActivity>,
    #[serde(default, alias = "legacy_model_recovery")]
    pub legacy_model_recovery: Option<LegacyModelRecovery>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportTask {
    pub job: ImportJob,
    pub document: Option<ImportedDocument>,
    pub stages: Vec<ImportStageRun>,
    #[serde(default, alias = "model_calls")]
    pub model_calls: Vec<ModelCall>,
    pub quarantine: Option<QuarantinedDocument>,
    #[serde(default, alias = "knowledge_analysis")]
    pub knowledge_analysis: Option<KnowledgeAnalysisProgress>,
    #[serde(default, alias = "import_progress")]
    pub import_progress: Vec<ImportProgressStep>,
    #[serde(default, alias = "model_usage")]
    pub model_usage: Vec<ModelUsageRecord>,
    #[serde(default, alias = "model_usage_aggregate")]
    pub model_usage_aggregate: Option<ImportUsageAggregate>,
    #[serde(default, alias = "model_activity")]
    pub model_activity: Option<ModelActivity>,
    #[serde(default, alias = "legacy_model_recovery")]
    pub legacy_model_recovery: Option<LegacyModelRecovery>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportJobsResult {
    pub jobs: Vec<ImportTask>,
    #[serde(default, alias = "page_tree_rebuilds")]
    pub page_tree_rebuilds: Vec<PageTreeRebuildTask>,
    #[serde(default, alias = "page_tree_enrichments")]
    pub page_tree_enrichments: Vec<PageTreeEnrichmentTask>,
    #[serde(default, alias = "knowledge_graph_extractions")]
    pub knowledge_graph_extractions: Vec<KnowledgeGraphExtractionTask>,
    #[serde(default, alias = "catalog_rebuild")]
    pub catalog_rebuild: Option<CatalogRebuildTask>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportControlResult {
    #[serde(alias = "job_id")]
    pub job_id: String,
    pub accepted: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", content = "data")]
pub enum KnowledgeBaseEvent {
    #[serde(rename = "knowledge_base.activated")]
    KnowledgeBaseActivated(KnowledgeBaseActivatedEventData),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeBaseActivatedEventData {
    #[serde(alias = "kb_dir")]
    pub kb_dir: String,
    pub name: String,
    #[serde(alias = "previous_kb_dir")]
    pub previous_kb_dir: Option<String>,
    pub checkpointed: bool,
}

#[derive(Deserialize)]
pub(crate) struct EngineHandshake {
    pub(crate) protocol_version: u32,
    pub(crate) engine_version: String,
}

#[derive(Deserialize)]
pub(crate) struct EngineHealthWire {
    pub(crate) status: EngineHealthStatus,
    pub(crate) protocol_version: u32,
    #[serde(default)]
    pub(crate) parser_readiness: BTreeMap<String, ParserReadiness>,
}

pub(crate) fn parse_response(message: Value) -> BridgeResult<Value> {
    if let Some(result) = message.get("result") {
        return Ok(result.clone());
    }
    let error = message.get("error");
    let code = error
        .and_then(|value| value.get("code"))
        .and_then(Value::as_str)
        .unwrap_or("engine_request_failed");
    let message = error
        .and_then(|value| value.get("message"))
        .and_then(Value::as_str)
        .unwrap_or("Python Engine returned an invalid response.");
    Err(BridgeError::new(code, message))
}

pub(crate) fn read_frame<R: Read>(reader: &mut R) -> BridgeResult<Option<Value>> {
    let mut prefix = [0_u8; 4];
    if !read_exact_or_eof(reader, &mut prefix)? {
        return Ok(None);
    }
    let size = u32::from_be_bytes(prefix) as usize;
    if size > MAX_FRAME_BYTES {
        return Err(BridgeError::new(
            "frame_too_large",
            format!("Desktop Bridge frame exceeds {MAX_FRAME_BYTES} bytes."),
        ));
    }
    let mut body = vec![0_u8; size];
    read_exact_or_eof(reader, &mut body)?
        .then_some(())
        .ok_or_else(|| {
            BridgeError::new(
                "truncated_frame",
                "Desktop Bridge frame ended unexpectedly.",
            )
        })?;
    let value = serde_json::from_slice::<Value>(&body).map_err(|error| {
        BridgeError::new(
            "invalid_frame",
            format!("Invalid Desktop Bridge JSON frame: {error}"),
        )
    })?;
    if !value.is_object() {
        return Err(BridgeError::new(
            "invalid_frame",
            "Desktop Bridge frame must contain an object.",
        ));
    }
    Ok(Some(value))
}

fn read_exact_or_eof<R: Read>(reader: &mut R, buffer: &mut [u8]) -> BridgeResult<bool> {
    let mut offset = 0;
    while offset < buffer.len() {
        let count = reader.read(&mut buffer[offset..]).map_err(|error| {
            BridgeError::new(
                "engine_unavailable",
                format!("Could not read Engine stream: {error}"),
            )
        })?;
        if count == 0 {
            if offset == 0 {
                return Ok(false);
            }
            return Err(BridgeError::new(
                "truncated_frame",
                "Desktop Bridge frame ended unexpectedly.",
            ));
        }
        offset += count;
    }
    Ok(true)
}

pub(crate) fn write_frame<W: Write>(writer: &mut W, value: &Value) -> BridgeResult<()> {
    let body = serde_json::to_vec(value).map_err(|error| {
        BridgeError::new(
            "invalid_frame",
            format!("Could not encode Desktop Bridge frame: {error}"),
        )
    })?;
    if body.len() > MAX_FRAME_BYTES {
        return Err(BridgeError::new(
            "frame_too_large",
            format!("Desktop Bridge frame exceeds {MAX_FRAME_BYTES} bytes."),
        ));
    }
    writer
        .write_all(&(body.len() as u32).to_be_bytes())
        .and_then(|_| writer.write_all(&body))
        .and_then(|_| writer.flush())
        .map_err(|error| {
            BridgeError::new(
                "engine_unavailable",
                format!("Could not write Engine stream: {error}"),
            )
        })
}
