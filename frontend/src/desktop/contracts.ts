/** The only React-facing contract for the Desktop Shell and Python Engine. */

export const DESKTOP_BRIDGE_PROTOCOL_VERSION = 1

export interface DesktopBridgeHandshake {
  protocolVersion: number
  engineVersion: string
}

export interface DesktopEngineHealth {
  status: "ready" | "starting" | "unavailable"
  protocolVersion: number
}

export type DesktopBridgeEvent =
  | DesktopEngineBridgeEvent
  | DesktopImportStageProgressEvent
  | DesktopGroundedAnswerDeltaEvent

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
  status: "running" | "paused" | "cancelled" | "recoverable" | "quarantined" | "completed" | "failed"
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
  stage: "preflight" | "raw_asset" | "document_ir" | "evidence" | "model_analysis" | "search"
  status: "pending" | "running" | "paused" | "cancelled" | "completed" | "failed" | "skipped"
  progress: number
  errorCode: string | null
}

export interface DesktopModelAttempt {
  attempt: number
  status: "running" | "retry_wait" | "completed" | "failed"
  timeoutSeconds: number
  remainingSeconds: number
  errorCode: string | null
  reason: string | null
}

export interface DesktopModelCall {
  callId: string
  stageRunId: string
  operation: string
  status: DesktopModelAttempt["status"]
  attemptCount: number
  timeoutSeconds: number
  nextTimeoutSeconds: number | null
  remainingSeconds: number
  errorCode: string | null
  reason: string | null
  suggestedAction: string | null
  attempts: DesktopModelAttempt[]
}

export interface DesktopQuarantinedDocument {
  stageRunId: string
  stage: DesktopImportStageRun["stage"]
  errorCode: string
  reason: string
  suggestedAction: string
  attemptCount: number
}

/** Optional settings used only by one manual recovery run. */
export interface DesktopRecoveryOverride {
  model?: string
  initialTimeoutSeconds?: number
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

export type DesktopRuntimeEvent =
  | { kind: "launch_intents_available" }
  | { kind: "tasks.requested" }
  | { kind: "engine.restarted" }
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
}

export interface DesktopImportTask {
  document: DesktopImportedDocument | null
  job: DesktopImportJob
  stages: DesktopImportStageRun[]
  modelCalls: DesktopModelCall[]
  quarantine: DesktopQuarantinedDocument | null
}

export interface DesktopImportJobs {
  jobs: DesktopImportTask[]
}

export interface DesktopRetrievalPlan {
  query: string
  terms: string[]
  source: "deterministic" | "model" | string
}

export interface DesktopEvidenceRef {
  evidenceId: string
  documentId: string
  documentName: string
  section: string
  locator: Record<string, unknown>
  excerpt: string
  channels: string[]
}

export interface DesktopAnswerSourceImage {
  sourceImageId: string
  evidenceId: string
  documentId: string
  documentName: string
  name: string
  mediaType: string
  filePath: string
  altText: string | null
  locator: Record<string, unknown>
}

export interface DesktopGroundedAnswer {
  answerId: string
  question: string
  answerText: string
  retrievalPlan: DesktopRetrievalPlan
  citations: DesktopEvidenceRef[]
  sourceImages: DesktopAnswerSourceImage[]
  degradations: string[]
  status: "completed" | "interrupted"
  interruptionCode: string | null
  interruptionReason: string | null
  createdAt: string
}

export interface DesktopGroundedAnswers {
  answers: DesktopGroundedAnswer[]
}

export type DesktopKnowledgePageKind = "concept" | "entity"

export interface DesktopKnowledgePageSummary {
  pageId: string
  kind: DesktopKnowledgePageKind
  title: string
  revisionNumber: number
  updatedAt: string
}

export interface DesktopKnowledgePage extends DesktopKnowledgePageSummary {
  contentMarkdown: string
  materializedPath: string
}

export interface DesktopKnowledgePages {
  pages: DesktopKnowledgePageSummary[]
}

export type DesktopDocumentVersionCandidateDecision = "link_to_candidate" | "keep_separate"

export interface DesktopDocumentVersionCandidate {
  candidateId: string
  documentId: string
  documentName: string
  candidateDocumentId: string
  candidateDocumentName: string
  lexicalScore: number
  characterScore: number
  reason: "lexical_character_similarity"
  status: "pending" | "accepted" | "rejected" | "dismissed"
}

export interface DesktopDocumentVersionCandidates {
  candidates: DesktopDocumentVersionCandidate[]
}

export type DesktopKnowledgeReconciliationBaselineKind = "published_generation" | "user_revision"
export type DesktopKnowledgeReconciliationDecision = "publish_incoming" | "keep_current"

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
  stagedDecision: DesktopKnowledgeReconciliationDecision | null
}

export interface DesktopKnowledgeReconciliationConflicts {
  conflicts: DesktopKnowledgeReconciliationConflict[]
}

export interface DesktopKnowledgeReconciliationCommit {
  publishedGenerationId: number | null
  publishedCount: number
  keptCount: number
  resolvedCandidateIds: string[]
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

/** KB-local model connection values passed only through the private Desktop Bridge. */
export interface DesktopModelSettings {
  model: string
  apiBaseUrl: string
  apiKey: string
  apiKeyConfigured: boolean
  maxConcurrentModelCalls: number
  initialTimeoutSeconds: number
  modelCallDeadlineSeconds: number
}

export interface DesktopDiagnosticBundle {
  path: string
  files: string[]
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
    model: string,
    apiBaseUrl: string,
    apiKey: string,
    maxConcurrentModelCalls: number,
    initialTimeoutSeconds: number,
    requestId: string,
  ): Promise<DesktopModelSettings>
  exportDiagnosticBundle(destination: string, requestId: string): Promise<DesktopDiagnosticBundle>
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
  importTextDocument(sourcePath: string, requestId: string): Promise<DesktopTextDocumentImport>
  importJobs(): Promise<DesktopImportJobs>
  askGrounded(question: string, requestId: string): Promise<DesktopGroundedAnswer>
  retryInterruptedAnswer(answerId: string, requestId: string): Promise<DesktopGroundedAnswer>
  groundedAnswers(): Promise<DesktopGroundedAnswers>
  knowledgePages(): Promise<DesktopKnowledgePages>
  getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage>
  saveKnowledgePage(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage>
  documentVersionCandidates(): Promise<DesktopDocumentVersionCandidates>
  resolveDocumentVersionCandidate(
    candidateId: string,
    decision: DesktopDocumentVersionCandidateDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCandidate>
  knowledgeReconciliationConflicts(): Promise<DesktopKnowledgeReconciliationConflicts>
  stageKnowledgeReconciliationDecisions(
    candidateIds: string[],
    decision: DesktopKnowledgeReconciliationDecision | null,
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationConflicts>
  commitKnowledgeReconciliationDecisions(
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationCommit>
  pauseImportJob(jobId: string): Promise<DesktopImportControlResult>
  resumeImportJob(jobId: string, requestId: string): Promise<DesktopTextDocumentImport>
  recoverImportJob(
    jobId: string,
    recoveryOverride: DesktopRecoveryOverride,
    requestId: string,
  ): Promise<DesktopTextDocumentImport>
  cancelImportJob(jobId: string): Promise<DesktopImportControlResult>
  cancel(targetRequestId: string): Promise<DesktopCancelResult>
  subscribe(listener: (event: DesktopBridgeEvent) => void): Promise<() => void>
}
