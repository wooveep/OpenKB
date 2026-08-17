import type {
  DesktopBridge,
  DesktopBridgeEvent,
  DesktopBridgeHandshake,
  DesktopCancelResult,
  DesktopDiagnosticBundle,
  DesktopImportControlResult,
  DesktopImportDropEvent,
  DesktopRuntimeEvent,
  DesktopRuntimeLaunchIntent,
  DesktopImportSourceInspection,
  DesktopImportSourcePicker,
  DesktopRawDocument,
  DesktopActiveKnowledgeBase,
  DesktopEngineHealth,
  DesktopGroundedAnswer,
  DesktopGroundedAnswers,
  DesktopKnowledgeBase,
  DesktopKnowledgeBaseActivation,
  DesktopKnowledgePage,
  DesktopKnowledgePages,
  DesktopKnowledgePageKind,
  DesktopModelSettings,
  DesktopDocumentVersionCandidate,
  DesktopDocumentVersionCandidates,
  DesktopDocumentVersionCandidateDecision,
  DesktopKnowledgeReconciliationConflict,
  DesktopKnowledgeReconciliationCommit,
  DesktopKnowledgeReconciliationConflicts,
  DesktopKnowledgeReconciliationDecision,
  DesktopImportTask,
  DesktopRecoveryOverride,
  DesktopTextDocumentImport,
} from "./contracts"

/** In-memory Bridge for React component tests; it never touches Tauri or Python. */
export class MemoryDesktopBridge implements DesktopBridge {
  private readonly listeners = new Set<(event: DesktopBridgeEvent) => void>()
  private readonly handshakeResult: DesktopBridgeHandshake
  private readonly healthResult: DesktopEngineHealth
  private activeKnowledgeBaseResult: DesktopKnowledgeBase | null = null
  private importJobResults: DesktopImportTask[] = []
  private groundedAnswerResults: DesktopGroundedAnswer[] = []
  private knowledgePageResults: DesktopKnowledgePage[] = []
  private documentVersionCandidateResults: DesktopDocumentVersionCandidate[] = []
  private knowledgeReconciliationConflictResults: DesktopKnowledgeReconciliationConflict[] = []
  private modelSettingsResult: DesktopModelSettings = {
    model: "gpt-5.4",
    apiBaseUrl: "https://api.openai.com/v1",
    apiKey: "",
    apiKeyConfigured: false,
    maxConcurrentModelCalls: 1,
    initialTimeoutSeconds: 20,
    modelCallDeadlineSeconds: 60,
  }

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

  async chooseKnowledgeBaseDirectory(): Promise<string | null> {
    return null
  }

  async revealKnowledgeBaseDirectory(kbDir: string): Promise<void> {
    void kbDir
  }

  async revealApplicationLogDirectory(): Promise<void> {
    return undefined
  }

  async modelSettings(): Promise<DesktopModelSettings> {
    return this.modelSettingsResult
  }

  async saveModelSettings(
    model: string,
    apiBaseUrl: string,
    apiKey: string,
    maxConcurrentModelCalls: number,
    initialTimeoutSeconds: number,
    requestId: string,
  ): Promise<DesktopModelSettings> {
    void requestId
    this.modelSettingsResult = {
      ...this.modelSettingsResult,
      model,
      apiBaseUrl,
      apiKey,
      apiKeyConfigured: Boolean(apiKey),
      maxConcurrentModelCalls,
      initialTimeoutSeconds,
    }
    return this.modelSettingsResult
  }

  async exportDiagnosticBundle(
    destination: string,
    requestId: string,
  ): Promise<DesktopDiagnosticBundle> {
    void requestId
    return {
      path: destination,
      files: ["manifest.json", "model-settings.json", "import-jobs.json", "model-calls.json"],
    }
  }

  async inspectImportSources(
    sourcePaths: string[],
    requestId: string,
  ): Promise<DesktopImportSourceInspection> {
    void requestId
    const supported = sourcePaths
      .filter(isSupportedImportSource)
      .sort((left, right) => left.localeCompare(right))
      .map((sourcePath) => ({
        path: sourcePath,
        name: sourceName(sourcePath),
        status: "supported" as const,
        errorCode: null,
      }))
    const unsupported = sourcePaths
      .filter((sourcePath) => !isSupportedImportSource(sourcePath))
      .sort((left, right) => left.localeCompare(right))
      .map((sourcePath) => ({
        path: sourcePath,
        name: sourceName(sourcePath),
        status: "unsupported" as const,
        errorCode: "unsupported_import_format",
      }))
    return {
      supported,
      unsupported,
      supportedExtensions: [".txt", ".md", ".markdown", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf"],
    }
  }

  async chooseImportSources(picker: DesktopImportSourcePicker): Promise<string[]> {
    void picker
    return []
  }

  async subscribeImportDrops(
    listener: (event: DesktopImportDropEvent) => void,
  ): Promise<() => void> {
    void listener
    return () => undefined
  }

  async readRawDocument(
    documentId: string,
    requestId: string,
    page = 0,
    focusLocator: Record<string, unknown> | undefined = undefined,
  ): Promise<DesktopRawDocument> {
    void requestId
    void focusLocator
    if (page !== 0) throw new Error("The requested document page was not found.")
    const document = this.importJobResults.find(
      (task) => task.document?.documentId === documentId,
    )?.document
    if (!document) throw new Error("The requested document was not found.")
    const content = `Original content for ${document.name}.`
    return {
      documentId: document.documentId,
      name: document.name,
      sourceFormat: document.sourceFormat,
      assetSha256: document.rawAssetSha256,
      byteSize: new TextEncoder().encode(content).byteLength,
      content,
      page,
      hasMore: false,
      sourceImages: [],
    }
  }

  async importTextDocument(
    sourcePath: string,
    requestId: string,
  ): Promise<DesktopTextDocumentImport> {
    if (this.activeKnowledgeBaseResult === null) {
      throw new Error("Open a Desktop Knowledge Base before importing a document.")
    }
    const name = sourceName(sourcePath) || "document.txt"
    const jobId = `job-${requestId}`
    const documentId = `document-${requestId}`
    const stages = ["preflight", "raw_asset", "document_ir", "evidence", "model_analysis", "search"] as const
    const progress = [20, 35, 55, 75, 85, 100]
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
          status: stage === "model_analysis" ? "skipped" : "completed",
          progress: progress[index],
          errorCode: null,
        },
      })
    }
    const result: DesktopTextDocumentImport = {
      document: {
        documentId,
        name,
        sourceFormat: sourceFormat(sourcePath),
        rawAssetSha256: "memory-bridge",
        evidenceCount: 1,
        availability: "available",
      },
      job: {
        jobId,
        sourceName: name,
        status: "completed",
        progress: 100,
        documentId,
        deduplicated: false,
        deduplication: null,
      },
      stages: stages.map((stage, index) => ({
        stageRunId: `${jobId}-${stage}`,
        stage,
        status: stage === "model_analysis" ? "skipped" : "completed",
        progress: progress[index],
        errorCode: null,
      })),
      modelCalls: [],
      quarantine: null,
    }
    this.importJobResults = [result, ...this.importJobResults]
    return result
  }

  async importJobs(): Promise<{ jobs: DesktopImportTask[] }> {
    return { jobs: this.importJobResults }
  }

  async askGrounded(question: string, requestId: string): Promise<DesktopGroundedAnswer> {
    if (this.activeKnowledgeBaseResult === null) {
      throw new Error("Open a Desktop Knowledge Base before asking a question.")
    }
    const source = this.importJobResults.find(
      (task) => task.document?.availability === "available",
    )?.document
    const citations = source ? [{
      evidenceId: `evidence-${source.documentId}`,
      documentId: source.documentId,
      documentName: source.name,
      section: "Document",
      locator: { ordinal: 0 },
      excerpt: `Original content for ${source.name}.`,
      channels: ["fts", "page_tree"],
    }] : []
    const answerId = `answer-${requestId}`
    const answerText = citations.length
      ? `Available source evidence for “${question}”:\n\n[1] ${citations[0].excerpt}`
      : `No available source evidence was found for: ${question}`
    this.emit({
      sequence: this.importJobResults.length + 1,
      kind: "answer.delta",
      data: { requestId, answerId, delta: answerText, replace: true, attempt: 1 },
    })
    const result: DesktopGroundedAnswer = {
      answerId,
      question,
      answerText,
      retrievalPlan: {
        query: question,
        terms: question.split(/\s+/).filter(Boolean),
        source: "deterministic",
      },
      citations,
      sourceImages: [],
      degradations: ["answer_model_unavailable"],
      status: "completed",
      interruptionCode: null,
      interruptionReason: null,
      createdAt: new Date().toISOString(),
    }
    this.groundedAnswerResults = [result, ...this.groundedAnswerResults]
    return result
  }

  async retryInterruptedAnswer(
    answerId: string,
    requestId: string,
  ): Promise<DesktopGroundedAnswer> {
    void requestId
    const existing = this.groundedAnswerResults.find((answer) => answer.answerId === answerId)
    if (!existing || existing.status !== "interrupted") {
      throw new Error("Only an interrupted answer can be retried.")
    }
    const result: DesktopGroundedAnswer = {
      ...existing,
      answerText: `Retried answer for “${existing.question}”.`,
      status: "completed",
      interruptionCode: null,
      interruptionReason: null,
    }
    this.groundedAnswerResults = this.groundedAnswerResults.map((answer) => (
      answer.answerId === answerId ? result : answer
    ))
    return result
  }

  async groundedAnswers(): Promise<DesktopGroundedAnswers> {
    return { answers: this.groundedAnswerResults }
  }

  async knowledgePages(): Promise<DesktopKnowledgePages> {
    return {
      pages: this.knowledgePageResults.map((page) => ({
        pageId: page.pageId,
        kind: page.kind,
        title: page.title,
        revisionNumber: page.revisionNumber,
        updatedAt: page.updatedAt,
      })),
    }
  }

  async getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage> {
    const page = this.knowledgePageResults.find((candidate) => candidate.pageId === pageId)
    if (!page) throw new Error("The requested knowledge page was not found.")
    return page
  }

  async saveKnowledgePage(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    if (this.activeKnowledgeBaseResult === null) {
      throw new Error("Open a Desktop Knowledge Base before editing knowledge pages.")
    }
    const now = new Date().toISOString()
    const existing = pageId === undefined
      ? undefined
      : this.knowledgePageResults.find((candidate) => candidate.pageId === pageId)
    if (pageId !== undefined && !existing) throw new Error("The requested knowledge page was not found.")
    if (existing && existing.kind !== kind) throw new Error("Knowledge page type cannot change.")
    const page: DesktopKnowledgePage = {
      pageId: existing?.pageId ?? `knowledge-page-${requestId}`,
      kind,
      title: title.trim(),
      contentMarkdown,
      revisionNumber: (existing?.revisionNumber ?? 0) + 1,
      materializedPath: existing?.materializedPath ?? `knowledge-pages/${kind}/${requestId}.md`,
      updatedAt: now,
    }
    this.knowledgePageResults = existing
      ? this.knowledgePageResults.map((candidate) => candidate.pageId === page.pageId ? page : candidate)
      : [page, ...this.knowledgePageResults]
    return page
  }

  async documentVersionCandidates(): Promise<DesktopDocumentVersionCandidates> {
    return {
      candidates: this.documentVersionCandidateResults.filter((candidate) => candidate.status === "pending"),
    }
  }

  async resolveDocumentVersionCandidate(
    candidateId: string,
    decision: DesktopDocumentVersionCandidateDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCandidate> {
    void requestId
    const candidate = this.documentVersionCandidateResults.find((item) => item.candidateId === candidateId)
    if (!candidate) throw new Error("The selected document version candidate was not found.")
    if (candidate.status !== "pending") throw new Error("The selected document version candidate is resolved.")
    const status: DesktopDocumentVersionCandidate["status"] = decision === "link_to_candidate"
      ? "accepted"
      : "rejected"
    const resolved = { ...candidate, status }
    this.documentVersionCandidateResults = this.documentVersionCandidateResults.map((item) => (
      item.documentId === candidate.documentId
        ? item.candidateId === candidateId ? resolved : { ...item, status: "dismissed" }
        : item
    ))
    return resolved
  }

  async knowledgeReconciliationConflicts(): Promise<DesktopKnowledgeReconciliationConflicts> {
    return { conflicts: this.knowledgeReconciliationConflictResults }
  }

  async stageKnowledgeReconciliationDecisions(
    candidateIds: string[],
    decision: DesktopKnowledgeReconciliationDecision | null,
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationConflicts> {
    void requestId
    const selected = new Set(candidateIds)
    if (!selected.size) throw new Error("Choose one or more knowledge conflicts first.")
    this.knowledgeReconciliationConflictResults = this.knowledgeReconciliationConflictResults.map((conflict) => (
      selected.has(conflict.candidateId) ? { ...conflict, stagedDecision: decision } : conflict
    ))
    return this.knowledgeReconciliationConflicts()
  }

  async commitKnowledgeReconciliationDecisions(
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationCommit> {
    void requestId
    const staged = this.knowledgeReconciliationConflictResults.filter(
      (conflict) => conflict.stagedDecision !== null,
    )
    if (!staged.length) throw new Error("Choose at least one knowledge conflict before committing.")
    const published = staged.filter((conflict) => conflict.stagedDecision === "publish_incoming")
    this.knowledgeReconciliationConflictResults = this.knowledgeReconciliationConflictResults.filter(
      (conflict) => conflict.stagedDecision === null,
    )
    return {
      publishedGenerationId: published.length ? 1 : null,
      publishedCount: published.length,
      keptCount: staged.length - published.length,
      resolvedCandidateIds: staged.map((conflict) => conflict.candidateId),
    }
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
        modelCalls: task.modelCalls,
        quarantine: task.quarantine,
      }
      this.importJobResults = [
        result,
        ...this.importJobResults.filter((item) => item.job.jobId !== jobId),
      ]
      return result
    }
    return this.importTextDocument(`resumed-${jobId}.txt`, requestId)
  }

  async recoverImportJob(
    jobId: string,
    recoveryOverride: DesktopRecoveryOverride,
    requestId: string,
  ): Promise<DesktopTextDocumentImport> {
    void recoveryOverride
    void requestId
    const task = this.importJobResults.find((item) => item.job.jobId === jobId)
    if (!task) throw new Error("This import task no longer exists.")
    const document = task.document ?? {
      documentId: `document-${jobId}`,
      name: `recovered-${jobId}.txt`,
      sourceFormat: "txt" as const,
      rawAssetSha256: "memory-bridge",
      evidenceCount: 1,
      availability: "available" as const,
    }
    const result: DesktopTextDocumentImport = {
      document,
      job: { ...task.job, status: "completed", progress: 100, documentId: document.documentId },
      stages: task.stages.map((stage) => ({
        ...stage,
        status: stage.status === "failed" || stage.status === "pending" ? "completed" : stage.status,
        progress: 100,
        errorCode: null,
      })),
      modelCalls: task.modelCalls,
      quarantine: null,
    }
    this.importJobResults = [result, ...this.importJobResults.filter((item) => item.job.jobId !== jobId)]
    return result
  }

  async cancelImportJob(jobId: string): Promise<DesktopImportControlResult> {
    this.updateImportTask(jobId, "cancelled")
    return { jobId, accepted: true }
  }

  async cancel(targetRequestId: string): Promise<DesktopCancelResult> {
    return { cancelled: true, requestId: targetRequestId }
  }

  async subscribeRuntimeEvents(
    listener: (event: DesktopRuntimeEvent) => void,
  ): Promise<() => void> {
    void listener
    return () => undefined
  }

  async takeLaunchIntents(): Promise<DesktopRuntimeLaunchIntent[]> {
    return []
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
      schemaVersion: 11,
      lastCheckpointAt: checkpointed ? new Date().toISOString() : null,
    }
    this.importJobResults = []
    this.groundedAnswerResults = []
    this.knowledgePageResults = []
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

function isSupportedImportSource(sourcePath: string): boolean {
  return /\.(txt|md|markdown|doc|docx|xls|xlsx|ppt|pptx|pdf)$/i.test(sourcePath)
}

function sourceFormat(sourcePath: string): "txt" | "markdown" | "doc" | "docx" | "xls" | "xlsx" | "ppt" | "pptx" | "pdf" {
  if (/\.(md|markdown)$/i.test(sourcePath)) return "markdown"
  if (/\.doc$/i.test(sourcePath)) return "doc"
  if (/\.docx$/i.test(sourcePath)) return "docx"
  if (/\.xlsx$/i.test(sourcePath)) return "xlsx"
  if (/\.xls$/i.test(sourcePath)) return "xls"
  if (/\.ppt$/i.test(sourcePath)) return "ppt"
  if (/\.pptx$/i.test(sourcePath)) return "pptx"
  if (/\.pdf$/i.test(sourcePath)) return "pdf"
  return "txt"
}

function sourceName(sourcePath: string): string {
  return sourcePath.split(/[\\/]/).filter(Boolean).at(-1) || ""
}
