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

export interface DesktopBridgeEvent {
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
  cancel(targetRequestId: string): Promise<DesktopCancelResult>
  subscribe(listener: (event: DesktopBridgeEvent) => void): Promise<() => void>
}
