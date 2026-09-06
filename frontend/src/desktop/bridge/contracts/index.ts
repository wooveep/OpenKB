import type { SemanticReviews, SemanticReviewDecision } from "./semantic-reviews"
/** The only React-facing contract for the Desktop Shell and Python Engine. */

import type {
  DesktopKnowledgeReanalysisOverview,
  DesktopKnowledgeReanalysisRun,
} from "@/desktop/bridge/contracts/knowledge-reanalysis-contracts"
import type { DesktopModelCallLifecycleEvent } from "@/desktop/bridge/contracts/model-call-lifecycle-contracts"
import type {
  DesktopImportProgressStep,
  DesktopKnowledgeAnalysisProgress,
  DesktopLegacyModelRecovery,
  DesktopModelActivity,
  DesktopModelCall,
  DesktopModelUsageAggregate,
  DesktopModelUsageRecord,
  DesktopRecoveryOverride,
} from "@/desktop/bridge/contracts/contracts-import-observability"
import type {
  DesktopModelConnectionTest,
  DesktopSaveAndVerifyModelConfiguration,
  DesktopModelSettings,
  DesktopModelSettingsDraft,
} from "@/desktop/bridge/contracts/model-settings-contracts"
import type {
  DesktopAnswerSourceImage,
  DesktopEvidenceRef,
  DesktopGroundedAnswer,
  DesktopGroundedAnswers,
  DesktopRetrievalPlan,
  DesktopRetrievalTrace,
} from "@/desktop/bridge/contracts/contracts-retrieval"
import type {
  DesktopKnowledgeAdoptionDecision,
  DesktopKnowledgeAdoptionResult,
  DesktopKnowledgePage,
  DesktopKnowledgePageDeletion,
  DesktopKnowledgePageKind,
  DesktopKnowledgePages,
  DesktopKnowledgeSourceCandidate,
  DesktopKnowledgeWorkspace,
  DesktopKnowledgeWorkspaceHistory,
  DesktopKnowledgeWorkspaceItem,
  DesktopKnowledgeWorkspaceItemRequest,
} from "@/desktop/bridge/contracts/knowledge-page-contracts"
import type {
  DesktopDocumentLineageDecision,
  DesktopDocumentVersionCandidate,
  DesktopDocumentVersionCandidateDecision,
  DesktopDocumentVersionCandidates,
  DesktopDocumentVersionCatalog,
  DesktopDocumentVersionDiffs,
  DesktopVersionFilter,
} from "@/desktop/bridge/contracts/contracts-document-versions"
export type * from "@/desktop/bridge/contracts/knowledge-reanalysis-contracts"
export type * from "@/desktop/bridge/contracts/model-call-lifecycle-contracts"
export type * from "@/desktop/bridge/contracts/contracts-import-observability"
export type * from "@/desktop/bridge/contracts/model-settings-contracts"
export type * from "@/desktop/bridge/contracts/contracts-retrieval"
export type * from "@/desktop/bridge/contracts/knowledge-page-contracts"
export type * from "@/desktop/bridge/contracts/contracts-document-versions"

export const DESKTOP_BRIDGE_PROTOCOL_VERSION = 1

export interface DesktopBridgeHandshake {
  protocolVersion: number
  engineVersion: string
}

export interface DesktopEngineHealth {
  status: "ready" | "starting" | "unavailable"
  protocolVersion: number
  parserReadiness?: Record<string, DesktopParserReadiness>
}

export interface DesktopParserReadiness {
  family: string
  formats: string[]
  resourceState: "resources_ready" | "unavailable"
  runtimeState: "not_loaded" | "initializing" | "ready" | "unavailable"
  diagnostic: string
}

export type DesktopBridgeEvent =
  | DesktopEngineBridgeEvent
  | DesktopImportStageProgressEvent
  | DesktopGroundedAnswerDeltaEvent
  | DesktopKnowledgeReanalysisUpdatedEvent
  | DesktopModelCallLifecycleEvent

export interface DesktopEngineBridgeEvent {
  sequence: number
  kind: "engine.request_started" | "engine.request_cancelled" | "engine.request_completed"
  data: DesktopEngineRequestEventData
}

export interface DesktopEngineRequestEventData {
  requestId: string
  ok?: boolean
  errorCode?: string
}

export interface DesktopCancelResult {
  cancelled: boolean
  requestId: string
}

export interface DesktopImportControlResult {
  jobId: string
  accepted: boolean
}

export interface DesktopImportStageProgressEvent {
  sequence: number
  kind: "import.stage_progress"
  data: DesktopImportStageRun & {
    requestId?: string | null
    jobId: string
    documentId?: string | null
  }
}

export interface DesktopGroundedAnswerDeltaEvent {
  sequence: number
  kind: "answer.delta"
  data: {
    requestId: string
    answerId: string
    delta: string
    replace: boolean
    attempt: number
  }
}

export interface DesktopKnowledgeReanalysisUpdatedEvent {
  sequence: number
  kind: "knowledge_reanalysis.updated"
  data: {
    runId: string
    jobId: string
  }
}

export interface DesktopImportedDocument {
  documentId: string
  name: string
  sourceFormat: string
  rawAssetSha256: string
  evidenceCount: number
  availability: "available" | "failed"
}

export interface DesktopImportJob {
  jobId: string
  sourceName: string
  status: "running" | "paused" | "cancelled" | "recoverable" | "quarantined" | "completed" | "failed" | "awaiting_model_configuration"
  progress: number
  documentId: string | null
  deduplicated: boolean
  deduplication: DesktopImportDeduplication | null
}

export interface DesktopImportDeduplication {
  level: "D0" | "D1" | "D2"
  reason: "raw_asset_sha256_match" | "normalized_body_sha256_match" | "evidence_sha256_match"
  reusedDocumentId: string | null
  reusedEvidenceCount: number
  reusableStages: DesktopImportStageRun["stage"][]
  normalizedBodySha256: string | null
}

export interface DesktopImportStageRun {
  stageRunId: string
  stage: "preflight" | "raw_asset" | "document_ir" | "evidence" | "deterministic_page_tree" | "model_analysis" | "search"
  status: "pending" | "running" | "paused" | "cancelled" | "completed" | "failed" | "skipped"
  progress: number
  errorCode: string | null
}

export interface DesktopQuarantinedDocument {
  stageRunId: string
  stage: DesktopImportStageRun["stage"]
  errorCode: string
  reason: string
  suggestedAction: string
  attemptCount: number
}


export interface DesktopImportSource {
  path: string
  name: string
  status: "supported" | "unsupported"
  errorCode: string | null
}

export interface DesktopImportSourceInspection {
  supported: DesktopImportSource[]
  unsupported: DesktopImportSource[]
  supportedExtensions: string[]
}

export type DesktopImportSourcePicker = "files" | "directory"

export interface DesktopImportDropEvent {
  type: "enter" | "over" | "drop" | "leave"
  paths: string[]
}

/** Shell-owned lifecycle actions forwarded into the existing workbench. */
export type DesktopRuntimeLaunchIntent =
  | { kind: "openKnowledgeBase"; kbDir: string }
  | { kind: "importSources"; sourcePaths: string[] }
  | { kind: "previousKnowledgeBaseUnavailable"; kbDir: string }
  | { kind: "activeKnowledgeBaseRestored" }

export type DesktopRuntimeEvent =
  | { kind: "launch_intents_available" }
  | { kind: "tasks.requested" }
  | { kind: "engine.restarted" }
  | { kind: "activeKnowledgeBaseRestored" }
  | { kind: "tray.restored" }

export interface DesktopRawDocument {
  documentId: string
  name: string
  sourceFormat: string
  assetSha256: string
  byteSize: number
  content: string
  page: number
  hasMore: boolean
  sourceImages: DesktopSourceImage[]
}

export interface DesktopSourceImage {
  sourceImageId: string
  name: string
  mediaType: string
  filePath: string
  altText: string | null
}

export interface DesktopTextDocumentImport {
  document: DesktopImportedDocument
  job: DesktopImportJob
  stages: DesktopImportStageRun[]
  modelCalls: DesktopModelCall[]
  quarantine: DesktopQuarantinedDocument | null
  knowledgeAnalysis?: DesktopKnowledgeAnalysisProgress | null
  importProgress: DesktopImportProgressStep[]
  modelUsage: DesktopModelUsageRecord[]
  modelUsageAggregate: DesktopModelUsageAggregate | null
  modelActivity: DesktopModelActivity | null
  legacyModelRecovery: DesktopLegacyModelRecovery | null
}

export interface DesktopImportTask {
  document: DesktopImportedDocument | null
  job: DesktopImportJob
  stages: DesktopImportStageRun[]
  modelCalls: DesktopModelCall[]
  quarantine: DesktopQuarantinedDocument | null
  knowledgeAnalysis?: DesktopKnowledgeAnalysisProgress | null
  importProgress: DesktopImportProgressStep[]
  modelUsage: DesktopModelUsageRecord[]
  modelUsageAggregate: DesktopModelUsageAggregate | null
  modelActivity: DesktopModelActivity | null
  legacyModelRecovery: DesktopLegacyModelRecovery | null
}

export interface DesktopImportJobs {
    jobs: DesktopImportTask[]
    pageTreeRebuilds: DesktopPageTreeRebuildTask[]
    pageTreeEnrichments: DesktopPageTreeEnrichmentTask[]
    knowledgeGraphExtractions: DesktopKnowledgeGraphExtractionTask[]
    catalogRebuild: DesktopCatalogRebuildTask | null
}

export interface DesktopCatalogRebuildTask {
  status: "pending" | "running" | "failed" | "completed"
  reason: string
  requestedSourceRevision: number
  attemptCount: number
  errorCode: string | null
  errorReason: string | null
  updatedAt: string
  completedAt: string | null
  currentGenerationId: string | null
  staleServing: boolean
  nodeCount: number
  linkCount: number
}

export interface DesktopPageTreeRebuildTask {
  documentId: string
  documentName: string
  status: "pending" | "running" | "failed" | "completed"
  reason: string
  errorCode: string | null
  attemptCount: number
  providerKind: string
  providerVersion: string
  updatedAt: string
  completedAt: string | null
  currentGenerationId: string | null
}

export interface DesktopPageTreeEnrichmentTask {
  documentId: string
  documentName: string
  status: "pending" | "running" | "failed" | "completed"
  reason: "initial" | "base_generation_update" | "model_update" | "prompt_update" | string
  provider: string
  model: string
  attemptCount: number
  modelAttempt: number
  callId: string | null
  errorCode: string | null
  errorReason: string | null
  updatedAt: string
  completedAt: string | null
  baseGenerationId: string
  currentEnrichmentGenerationId: string | null
  modelActivity: DesktopModelActivity | null
}

export interface DesktopPageTreeEnrichmentControlResult {
  documentId: string
  accepted: boolean
}

export interface DesktopKnowledgeGraphExtractionTask {
  documentId: string
  documentName: string
  status: "pending" | "running" | "failed" | "completed" | "completed_empty"
  nodeCount: number
  edgeCount: number
  quality: "full" | "degraded" | null
  retainedCount: number
  weakenedCount: number
  rejectedCount: number
  reason: string
  provider: string
  model: string
  attemptCount: number
  modelAttempt: number
  callId: string | null
  errorCode: string | null
  errorReason: string | null
  updatedAt: string
  completedAt: string | null
  modelActivity: DesktopModelActivity | null
}

export interface DesktopKnowledgeGraphExtractionControlResult {
  documentId: string
  accepted: boolean
}

export interface DesktopConversationSummary {
  conversationId: string
  title: string
  draftText: string
  createdAt: string
  updatedAt: string
  generating: boolean
}

export interface DesktopConversationList {
  conversations: DesktopConversationSummary[]
  lastConversationId: string | null
}

export interface DesktopConversationEvidenceRef extends DesktopEvidenceRef {
  sourceAvailable: boolean
}

export interface DesktopConversationSourceImage extends DesktopAnswerSourceImage {
  sourceAvailable: boolean
}

export interface DesktopAnswerVersion {
  answerVersionId: string
  versionNumber: number
  answerText: string
  retrievalPlan: DesktopRetrievalPlan
  citations: DesktopConversationEvidenceRef[]
  sourceImages: DesktopConversationSourceImage[]
  retrievalTrace: DesktopRetrievalTrace
  degradations: string[]
  status: "completed" | "interrupted"
  interruptionCode: string | null
  interruptionReason: string | null
  createdAt: string
}

export interface DesktopConversationMessage {
  messageId: string
  ordinal: number
  role: "user" | "assistant"
  content: string
  status: "completed" | "generating" | "interrupted"
  selectedAnswerVersionId: string | null
  createdAt: string
  updatedAt: string
  answerVersions: DesktopAnswerVersion[]
}

export interface DesktopConversation extends Omit<DesktopConversationSummary, "generating"> {
  messages: DesktopConversationMessage[]
}

export type DesktopGlobalSearchResultKind = "document" | "knowledge_page" | "conversation"

export interface DesktopGlobalSearchResult {
  resultId: string
  kind: DesktopGlobalSearchResultKind
  title: string
  snippet: string
  status: "available" | "failed"
  documentId: string | null
  pageId: string | null
  conversationId: string | null
  messageId: string | null
}

export interface DesktopGlobalSearchResults {
  query: string
  results: DesktopGlobalSearchResult[]
}

export type DesktopKnowledgeReconciliationBaselineKind =
  | "published_generation"
  | "user_revision"
  | "unpublished_page"
export type DesktopKnowledgeReconciliationMode = "two_way" | "three_way"
export type DesktopKnowledgeReconciliationDecision =
  | "publish_incoming"
  | "keep_current"
  | "keep_draft"
  | "apply_incoming"
  | "replace_draft"
  | "manual_merge"

export interface DesktopKnowledgeReconciliationConflict {
  candidateId: string
  documentId: string
  documentName: string
  kind: DesktopKnowledgePageKind
  title: string
  contentMarkdown: string
  baselineKind: DesktopKnowledgeReconciliationBaselineKind
  baselineTitle: string
  baselineContentMarkdown: string
  observedGenerationId: number | null
  reconciliationMode: DesktopKnowledgeReconciliationMode
  targetPageId: string | null
  workingDraftTitle: string | null
  workingDraftContentMarkdown: string | null
  workingDraftUpdatedAt: string | null
  stagedDecision: DesktopKnowledgeReconciliationDecision | null
  stagedContentMarkdown: string | null
}

export interface DesktopKnowledgeReconciliationConflicts {
  conflicts: DesktopKnowledgeReconciliationConflict[]
}

export interface DesktopKnowledgeReconciliationCommit {
  publishedGenerationId: number | null
  publishedCount: number
  draftUpdatedCount: number
  keptCount: number
  resolvedCandidateIds: string[]
}

export type DesktopMissingSourceReason =
  | "source_not_provided"
  | "source_reference_unresolved"
export type DesktopMissingSourceOutcome =
  | "working_draft"
  | "generated"
  | "review_required"
  | "deduplicated"
  | "dismissed"

export interface DesktopMissingSourceCandidate {
  candidateId: string
  category: "missing_source"
  documentId: string
  documentName: string
  kind: DesktopKnowledgePageKind
  title: string
  claimText: string
  reason: DesktopMissingSourceReason
  section: string
  locator: Record<string, unknown>
  createdAt: string
}

export interface DesktopMissingSourceCandidates {
  candidates: DesktopMissingSourceCandidate[]
}

export interface DesktopMissingSourceBinding {
  candidateId: string
  decision: "bound"
  outcome: DesktopMissingSourceOutcome
  remainingCount: number
}

export interface DesktopMissingSourceDismissal {
  decision: "dismissed"
  resolvedCandidateIds: string[]
  remainingCount: number
}

/** The SQLite-authoritative knowledge base currently available to the Desktop Runtime. */
export interface DesktopKnowledgeBase {
  kbDir: string
  name: string
  schemaVersion: number
  lastCheckpointAt: string | null
}

export interface DesktopKnowledgeBaseActivatedEvent {
  kind: "knowledge_base.activated"
  data: {
    kbDir: string
    name: string
    previousKbDir: string | null
    checkpointed: boolean
  }
}

export interface DesktopKnowledgeBaseActivation {
  knowledgeBase: DesktopKnowledgeBase
  events: DesktopKnowledgeBaseActivatedEvent[]
}

export interface DesktopActiveKnowledgeBase {
  knowledgeBase: DesktopKnowledgeBase | null
}

export class DesktopBridgeError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "DesktopBridgeError"
    this.code = code
  }
}

export interface DesktopDiagnosticBundle {
  path: string
  files: string[]
}

export type DesktopKnowledgeExportMode =
  | "knowledge_projection"
  | "self_contained"
  | "portable_wiki"

export interface DesktopKnowledgeExport {
  path: string
  mode: DesktopKnowledgeExportMode
  files: string[]
  rawAssetCount: number
  sourceImageCount: number
}

export interface DesktopKnowledgeExportPreview {
  mode: DesktopKnowledgeExportMode
  documentCount: number
  estimatedSizeBytes: number
  snapshotId: string
}

export interface DesktopBridge {
  handshake(): Promise<DesktopBridgeHandshake>
  health(): Promise<DesktopEngineHealth>
  createKnowledgeBase(
    kbDir: string,
    name: string | undefined,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseActivation>
  openKnowledgeBase(kbDir: string, requestId: string): Promise<DesktopKnowledgeBaseActivation>
  activeKnowledgeBase(): Promise<DesktopActiveKnowledgeBase>
  chooseKnowledgeBaseDirectory(): Promise<string | null>
  revealKnowledgeBaseDirectory(kbDir: string): Promise<void>
  revealApplicationLogDirectory(): Promise<void>
  modelSettings(): Promise<DesktopModelSettings>
  saveModelSettings(
    settings: DesktopModelSettingsDraft,
    requestId: string,
  ): Promise<DesktopModelSettings>
  saveAndVerifyModelSettings(
    settings: DesktopModelSettingsDraft,
    requestId: string,
  ): Promise<DesktopSaveAndVerifyModelConfiguration>
  testModelConnection(
    settings: DesktopModelSettingsDraft,
    requestId: string,
  ): Promise<DesktopModelConnectionTest>
  exportDiagnosticBundle(destination: string, requestId: string): Promise<DesktopDiagnosticBundle>
  exportKnowledgeBundle(
    destination: string,
    mode: DesktopKnowledgeExportMode,
    requestId: string,
    expectedSnapshotId?: string,
  ): Promise<DesktopKnowledgeExport>
  previewKnowledgeBundle(
    mode: DesktopKnowledgeExportMode,
  ): Promise<DesktopKnowledgeExportPreview>
  inspectImportSources(
    sourcePaths: string[],
    requestId: string,
  ): Promise<DesktopImportSourceInspection>
  chooseImportSources(picker: DesktopImportSourcePicker): Promise<string[]>
  subscribeImportDrops(
    listener: (event: DesktopImportDropEvent) => void,
  ): Promise<() => void>
  takeLaunchIntents(): Promise<DesktopRuntimeLaunchIntent[]>
  subscribeRuntimeEvents(listener: (event: DesktopRuntimeEvent) => void): Promise<() => void>
  readRawDocument(
    documentId: string,
    requestId: string,
    page?: number,
    focusLocator?: Record<string, unknown>,
  ): Promise<DesktopRawDocument>
  importTextDocument(sourcePath: string, requestId: string, parserMode?: "auto" | "fast" | "enhanced"): Promise<DesktopTextDocumentImport>
  importJobs(): Promise<DesktopImportJobs>
  knowledgeReanalysis(): Promise<DesktopKnowledgeReanalysisOverview>
  startKnowledgeReanalysis(
    documentIds: string[],
    requestId: string,
  ): Promise<DesktopKnowledgeReanalysisRun>
  retryKnowledgeReanalysis(jobId: string, requestId: string): Promise<DesktopKnowledgeReanalysisRun>
  askGrounded(
    question: string,
    requestId: string,
    versionFilter?: DesktopVersionFilter,
  ): Promise<DesktopGroundedAnswer>
  retryInterruptedAnswer(answerId: string, requestId: string): Promise<DesktopGroundedAnswer>
  groundedAnswers(): Promise<DesktopGroundedAnswers>
  conversations(search?: string): Promise<DesktopConversationList>
  globalSearch(query: string): Promise<DesktopGlobalSearchResults>
  getConversation(conversationId: string): Promise<DesktopConversation>
  createConversation(title: string | undefined, requestId: string): Promise<DesktopConversation>
  renameConversation(conversationId: string, title: string, requestId: string): Promise<DesktopConversation>
  deleteConversation(conversationId: string, requestId: string): Promise<DesktopConversationList>
  saveConversationDraft(conversationId: string, draftText: string, requestId: string): Promise<DesktopConversation>
  askConversation(
    conversationId: string,
    question: string,
    requestId: string,
    versionFilter?: DesktopVersionFilter,
  ): Promise<DesktopConversation>
  regenerateConversationAnswer(conversationId: string, assistantMessageId: string, requestId: string): Promise<DesktopConversation>
  selectAnswerVersion(conversationId: string, assistantMessageId: string, answerVersionId: string, requestId: string): Promise<DesktopConversation>
  knowledgeWorkspace(query?: string): Promise<DesktopKnowledgeWorkspace>
  getKnowledgeWorkspaceItem(
    item: DesktopKnowledgeWorkspaceItemRequest,
  ): Promise<DesktopKnowledgeWorkspaceItem>
  knowledgeWorkspaceHistory(generationId?: number): Promise<DesktopKnowledgeWorkspaceHistory>
  adoptKnowledgeItem(
    generationId: number,
    itemKey: string,
    adoptionRequestId: string,
    requestId: string,
    decision?: DesktopKnowledgeAdoptionDecision,
    candidatePageId?: string,
  ): Promise<DesktopKnowledgeAdoptionResult>
  knowledgePages(): Promise<DesktopKnowledgePages>
  getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage>
  saveKnowledgePage(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage>
  publishKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage>
  verifyKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage>
  setKnowledgePageStaleAfter(pageId: string, staleAfter: string | null, requestId: string): Promise<DesktopKnowledgePage>
  deprecateKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage>
  restoreKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage>
  permanentlyDeleteKnowledgePage(pageId: string, confirmationPageId: string, requestId: string): Promise<DesktopKnowledgePageDeletion>
  searchKnowledgeSources(query: string): Promise<DesktopKnowledgeSourceCandidate[]>
  bindKnowledgePageSource(
    pageId: string,
    claimText: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage>
  documentVersionCandidates(): Promise<DesktopDocumentVersionCandidates>
  documentVersionCatalog(): Promise<DesktopDocumentVersionCatalog>
  confirmDocumentLineage(
    decision: DesktopDocumentLineageDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCatalog>
  documentVersionDiffs(lineageId: string): Promise<DesktopDocumentVersionDiffs>
  resolveDocumentVersionCandidate(
    candidateId: string,
    decision: DesktopDocumentVersionCandidateDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCandidate>
  semanticReviews(): Promise<SemanticReviews>
  resolveSemanticReview(reviewId: string, decision: SemanticReviewDecision, requestId: string): Promise<SemanticReviews>
  knowledgeReconciliationConflicts(): Promise<DesktopKnowledgeReconciliationConflicts>
  stageKnowledgeReconciliationDecisions(
    candidateIds: string[],
    decision: DesktopKnowledgeReconciliationDecision | null,
    manualMergeContent: string | null,
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationConflicts>
  commitKnowledgeReconciliationDecisions(
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationCommit>
  missingSourceCandidates(): Promise<DesktopMissingSourceCandidates>
  bindMissingSourceCandidate(
    candidateId: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopMissingSourceBinding>
  dismissMissingSourceCandidates(
    candidateIds: string[],
    requestId: string,
  ): Promise<DesktopMissingSourceDismissal>
  pauseImportJob(jobId: string): Promise<DesktopImportControlResult>
  resumeImportJob(jobId: string, requestId: string): Promise<DesktopTextDocumentImport>
  recoverImportJob(
    jobId: string,
    recoveryOverride: DesktopRecoveryOverride,
    requestId: string,
  ): Promise<DesktopTextDocumentImport>
  cancelImportJob(jobId: string): Promise<DesktopImportControlResult>
  cancelPageTreeEnrichment(
    documentId: string,
  ): Promise<DesktopPageTreeEnrichmentControlResult>
  retryPageTreeEnrichment(
    documentId: string,
  ): Promise<DesktopPageTreeEnrichmentControlResult>
  cancelKnowledgeGraphExtraction(
    documentId: string,
  ): Promise<DesktopKnowledgeGraphExtractionControlResult>
  retryKnowledgeGraphExtraction(
    documentId: string,
  ): Promise<DesktopKnowledgeGraphExtractionControlResult>
  cancel(targetRequestId: string): Promise<DesktopCancelResult>
  subscribe(listener: (event: DesktopBridgeEvent) => void): Promise<() => void>
}
