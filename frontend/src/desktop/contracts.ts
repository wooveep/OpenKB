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
  createdAt: string
}

export interface DesktopGroundedAnswers {
  answers: DesktopGroundedAnswer[]
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

export interface DesktopKnowledgeBaseSnapshot {
  kbDir: string
  inventory: DesktopKnowledgeBaseInventory
  status: DesktopKnowledgeBaseStatus
}

export interface DesktopKnowledgeBaseInventory {
  documents: DesktopKnowledgeBaseDocument[]
  documentCount: number
  summaries: string[]
  concepts: string[]
  entities: string[]
  reports: string[]
}

export interface DesktopKnowledgeBaseDocument {
  hash: string
  name: string
  type: string
  displayType: string
  pages: number | null
}

export interface DesktopKnowledgeBaseStatus {
  directories: {
    sources: number
    summaries: number
    concepts: number
    reports: number
  }
  rawCount: number
  totalIndexed: number
  lastCompile: string | null
  lastLint: string | null
}

export interface DesktopKnowledgeBaseInspectedEvent {
  kind: "knowledge_base.inspected"
  data: {
    kbDir: string
    documentCount: number
  }
}

export interface DesktopKnowledgeBaseInspection {
  snapshot: DesktopKnowledgeBaseSnapshot
  events: DesktopKnowledgeBaseInspectedEvent[]
}

export class DesktopBridgeError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "DesktopBridgeError"
    this.code = code
  }
}

export interface DesktopBridge {
  handshake(): Promise<DesktopBridgeHandshake>
  health(): Promise<DesktopEngineHealth>
  inspectKnowledgeBase(
    kbDir: string,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseInspection>
  createKnowledgeBase(
    kbDir: string,
    name: string | undefined,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseActivation>
  openKnowledgeBase(kbDir: string, requestId: string): Promise<DesktopKnowledgeBaseActivation>
  activeKnowledgeBase(): Promise<DesktopActiveKnowledgeBase>
  inspectImportSources(
    sourcePaths: string[],
    requestId: string,
  ): Promise<DesktopImportSourceInspection>
  chooseImportSources(picker: DesktopImportSourcePicker): Promise<string[]>
  subscribeImportDrops(
    listener: (event: DesktopImportDropEvent) => void,
  ): Promise<() => void>
  readRawDocument(
    documentId: string,
    requestId: string,
    page?: number,
    focusLocator?: Record<string, unknown>,
  ): Promise<DesktopRawDocument>
  importTextDocument(sourcePath: string, requestId: string): Promise<DesktopTextDocumentImport>
  importJobs(): Promise<DesktopImportJobs>
  askGrounded(question: string, requestId: string): Promise<DesktopGroundedAnswer>
  groundedAnswers(): Promise<DesktopGroundedAnswers>
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
