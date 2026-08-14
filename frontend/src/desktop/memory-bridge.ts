import type {
  DesktopBridge,
  DesktopBridgeEvent,
  DesktopBridgeHandshake,
  DesktopCancelResult,
  DesktopEngineHealth,
  DesktopKnowledgeBaseInspection,
} from "./contracts"

/** In-memory Bridge for React component tests; it never touches Tauri or Python. */
export class MemoryDesktopBridge implements DesktopBridge {
  private readonly listeners = new Set<(event: DesktopBridgeEvent) => void>()
  private readonly handshakeResult: DesktopBridgeHandshake
  private readonly healthResult: DesktopEngineHealth

  constructor(
    handshakeResult: DesktopBridgeHandshake = {
      protocolVersion: 1,
      engineVersion: "test",
    },
    healthResult: DesktopEngineHealth = {
      status: "ready",
      protocolVersion: 1,
    },
  ) {
    this.handshakeResult = handshakeResult
    this.healthResult = healthResult
  }

  async handshake(): Promise<DesktopBridgeHandshake> {
    return this.handshakeResult
  }

  async health(): Promise<DesktopEngineHealth> {
    return this.healthResult
  }

  async inspectKnowledgeBase(
    kbDir: string,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseInspection> {
    void requestId
    return {
      snapshot: {
        kbDir,
        inventory: {
          documents: [],
          documentCount: 0,
          summaries: [],
          concepts: [],
          entities: [],
          reports: [],
        },
        status: {
          directories: { sources: 0, summaries: 0, concepts: 0, reports: 0 },
          rawCount: 0,
          totalIndexed: 0,
          lastCompile: null,
          lastLint: null,
        },
      },
      events: [
        {
          kind: "knowledge_base.inspected",
          data: { kbDir, documentCount: 0 },
        },
      ],
    }
  }

  async cancel(targetRequestId: string): Promise<DesktopCancelResult> {
    return { cancelled: true, requestId: targetRequestId }
  }

  async subscribe(listener: (event: DesktopBridgeEvent) => void): Promise<() => void> {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  emit(event: DesktopBridgeEvent): void {
    for (const listener of this.listeners) listener(event)
  }
}
