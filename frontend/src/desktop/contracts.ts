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

export type DesktopBridgeEvent = DesktopEngineBridgeEvent | DesktopImportStageProgressEvent

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

export interface DesktopImportStageProgressEvent {
  sequence: number
  kind: "import.stage_progress"
  data: DesktopImportStageRun & {
    requestId?: string | null
    jobId: string
    documentId?: string | null
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
  status: "running" | "completed" | "failed"
  progress: number
  documentId: string | null
  deduplicated: boolean
}

export interface DesktopImportStageRun {
  stageRunId: string
  stage: "preflight" | "raw_asset" | "document_ir" | "evidence" | "search"
  status: "pending" | "running" | "completed" | "failed" | "skipped"
  progress: number
  errorCode: string | null
}

export interface DesktopTextDocumentImport {
  document: DesktopImportedDocument
  job: DesktopImportJob
  stages: DesktopImportStageRun[]
}

export interface DesktopImportTask {
  document: DesktopImportedDocument | null
  job: DesktopImportJob
  stages: DesktopImportStageRun[]
}

export interface DesktopImportJobs {
  jobs: DesktopImportTask[]
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
  importTextDocument(sourcePath: string, requestId: string): Promise<DesktopTextDocumentImport>
  importJobs(): Promise<DesktopImportJobs>
  cancel(targetRequestId: string): Promise<DesktopCancelResult>
  subscribe(listener: (event: DesktopBridgeEvent) => void): Promise<() => void>
}
