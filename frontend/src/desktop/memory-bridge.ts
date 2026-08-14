import type {
  DesktopBridge,
  DesktopBridgeEvent,
  DesktopBridgeHandshake,
  DesktopCancelResult,
  DesktopImportControlResult,
  DesktopActiveKnowledgeBase,
  DesktopEngineHealth,
  DesktopKnowledgeBase,
  DesktopKnowledgeBaseActivation,
  DesktopKnowledgeBaseInspection,
  DesktopImportTask,
  DesktopTextDocumentImport,
} from "./contracts"

/** In-memory Bridge for React component tests; it never touches Tauri or Python. */
export class MemoryDesktopBridge implements DesktopBridge {
  private readonly listeners = new Set<(event: DesktopBridgeEvent) => void>()
  private readonly handshakeResult: DesktopBridgeHandshake
  private readonly healthResult: DesktopEngineHealth
  private activeKnowledgeBaseResult: DesktopKnowledgeBase | null = null
  private importJobResults: DesktopImportTask[] = []

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

  async createKnowledgeBase(
    kbDir: string,
    name: string | undefined,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseActivation> {
    void requestId
    return this.activate(kbDir, name || "Untitled knowledge base")
  }

  async openKnowledgeBase(
    kbDir: string,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseActivation> {
    void requestId
    return this.activate(kbDir, kbDir.split(/[\\/]/).filter(Boolean).at(-1) || "Knowledge base")
  }

  async activeKnowledgeBase(): Promise<DesktopActiveKnowledgeBase> {
    return { knowledgeBase: this.activeKnowledgeBaseResult }
  }

  async importTextDocument(
    sourcePath: string,
    requestId: string,
  ): Promise<DesktopTextDocumentImport> {
    if (this.activeKnowledgeBaseResult === null) {
      throw new Error("Open a Desktop Knowledge Base before importing a document.")
    }
    const name = sourcePath.split(/[\\/]/).filter(Boolean).at(-1) || "document.txt"
    const jobId = `job-${requestId}`
    const documentId = `document-${requestId}`
    const stages = ["preflight", "raw_asset", "document_ir", "evidence", "search"] as const
    for (const [index, stage] of stages.entries()) {
      this.emit({
        sequence: index + 1,
        kind: "import.stage_progress",
        data: {
          requestId,
          jobId,
          documentId: stage === "search" ? documentId : undefined,
          stageRunId: `${jobId}-${stage}`,
          stage,
          status: "completed",
          progress: (index + 1) * 20,
          errorCode: null,
        },
      })
    }
    const result: DesktopTextDocumentImport = {
      document: {
        documentId,
        name,
        sourceFormat: "txt",
        rawAssetSha256: "memory-bridge",
        evidenceCount: 1,
        availability: "available",
      },
      job: {
        jobId,
        status: "completed",
        progress: 100,
        documentId,
        deduplicated: false,
      },
      stages: stages.map((stage, index) => ({
        stageRunId: `${jobId}-${stage}`,
        stage,
        status: "completed",
        progress: (index + 1) * 20,
        errorCode: null,
      })),
    }
    this.importJobResults = [result, ...this.importJobResults]
    return result
  }

  async importJobs(): Promise<{ jobs: DesktopImportTask[] }> {
    return { jobs: this.importJobResults }
  }

  async pauseImportJob(jobId: string): Promise<DesktopImportControlResult> {
    this.updateImportTask(jobId, "paused")
    return { jobId, accepted: true }
  }

  async resumeImportJob(jobId: string, requestId: string): Promise<DesktopTextDocumentImport> {
    const task = this.importJobResults.find((item) => item.job.jobId === jobId)
    if (task?.document) {
      const result: DesktopTextDocumentImport = {
        document: task.document,
        job: { ...task.job, status: "completed", progress: 100 },
        stages: task.stages.map((stage) => ({
          ...stage,
          status: "completed",
          progress: 100,
          errorCode: null,
        })),
      }
      this.importJobResults = [
        result,
        ...this.importJobResults.filter((item) => item.job.jobId !== jobId),
      ]
      return result
    }
    return this.importTextDocument(`resumed-${jobId}.txt`, requestId)
  }

  async cancelImportJob(jobId: string): Promise<DesktopImportControlResult> {
    this.updateImportTask(jobId, "cancelled")
    return { jobId, accepted: true }
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

  private activate(kbDir: string, name: string): DesktopKnowledgeBaseActivation {
    const previousKbDir = this.activeKnowledgeBaseResult?.kbDir ?? null
    const checkpointed = previousKbDir !== null && previousKbDir !== kbDir
    this.activeKnowledgeBaseResult = {
      kbDir,
      name,
      schemaVersion: 3,
      lastCheckpointAt: checkpointed ? new Date().toISOString() : null,
    }
    this.importJobResults = []
    return {
      knowledgeBase: this.activeKnowledgeBaseResult,
      events: [
        {
          kind: "knowledge_base.activated",
          data: { kbDir, name, previousKbDir, checkpointed },
        },
      ],
    }
  }

  private updateImportTask(jobId: string, status: "paused" | "cancelled"): void {
    this.importJobResults = this.importJobResults.map((task) => {
      if (task.job.jobId !== jobId) return task
      const activeStage = task.stages.find((stage) => stage.status === "running")
        ?? task.stages.find((stage) => stage.status === "pending")
      return {
        ...task,
        job: { ...task.job, status },
        stages: task.stages.map((stage) => (
          stage.stageRunId === activeStage?.stageRunId
            ? { ...stage, status, errorCode: `import_${status}` }
            : stage
        )),
      }
    })
  }
}
