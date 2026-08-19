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
  DesktopConversation,
  DesktopConversationList,
  DesktopKnowledgeBase,
  DesktopKnowledgeBaseActivation,
  DesktopKnowledgePage,
  DesktopKnowledgePages,
  DesktopKnowledgePageKind,
  DesktopKnowledgeSourceCandidate,
  DesktopGlobalSearchResults,
  DesktopModelSettings,
  DesktopModelConnectionTest,
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
import {
  emitBridgeEvent,
  isSupportedImportSource,
  requireConversation,
  sourceFormat,
  sourceName,
  updateImportTasks,
} from "./memory-bridge-helpers"
import { MemoryKnowledgePageStore } from "./memory-knowledge-pages"

/** In-memory Bridge for React component tests; it never touches Tauri or Python. */
export class MemoryDesktopBridge implements DesktopBridge {
  private readonly listeners = new Set<(event: DesktopBridgeEvent) => void>()
  private readonly handshakeResult: DesktopBridgeHandshake
  private readonly healthResult: DesktopEngineHealth
  private activeKnowledgeBaseResult: DesktopKnowledgeBase | null = null
  private importJobResults: DesktopImportTask[] = []
  private groundedAnswerResults: DesktopGroundedAnswer[] = []
  private conversationResults: DesktopConversation[] = []
  private lastConversationId: string | null = null
  private readonly knowledgePagesStore = new MemoryKnowledgePageStore()
  private documentVersionCandidateResults: DesktopDocumentVersionCandidate[] = []
  private knowledgeReconciliationConflictResults: DesktopKnowledgeReconciliationConflict[] = []
  private modelSettingsResult: DesktopModelSettings = {
    provider: "custom",
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
    provider: string,
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
      provider,
      model,
      apiBaseUrl,
      apiKey,
      apiKeyConfigured: Boolean(apiKey),
      maxConcurrentModelCalls,
      initialTimeoutSeconds,
    }
    return this.modelSettingsResult
  }

  async testModelConnection(
    provider: string,
    model: string,
    apiBaseUrl: string,
    apiKey: string,
    maxConcurrentModelCalls: number,
    initialTimeoutSeconds: number,
    requestId: string,
  ): Promise<DesktopModelConnectionTest> {
    void provider
    void apiBaseUrl
    void apiKey
    void maxConcurrentModelCalls
    void initialTimeoutSeconds
    void requestId
    return { ok: true, model, latencyMs: 42, attemptCount: 1 }
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
      emitBridgeEvent(this.listeners, {
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
      channels: ["fts", "structure_lexical"],
    }] : []
    const answerId = `answer-${requestId}`
    const answerText = citations.length
      ? `Available source evidence for “${question}”:\n\n[1] ${citations[0].excerpt}`
      : `No available source evidence was found for: ${question}`
    emitBridgeEvent(this.listeners, {
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

  async conversations(search = ""): Promise<DesktopConversationList> {
    const normalized = search.trim().toLowerCase()
    const conversations = this.conversationResults.filter((conversation) => (
      !normalized
      || conversation.title.toLowerCase().includes(normalized)
      || conversation.messages.some((message) => message.role === "user" && message.content.toLowerCase().includes(normalized))
    )).map((conversation) => ({
      conversationId: conversation.conversationId,
      title: conversation.title,
      draftText: conversation.draftText,
      createdAt: conversation.createdAt,
      updatedAt: conversation.updatedAt,
      generating: conversation.messages.some((message) => message.status === "generating"),
    }))
    return { conversations, lastConversationId: this.lastConversationId }
  }

  async globalSearch(query: string): Promise<DesktopGlobalSearchResults> {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return { query: "", results: [] }
    const conversations = this.conversationResults.filter((item) => (
      item.title.toLowerCase().includes(normalized)
      || item.messages.some((message) => message.role === "user" && message.content.toLowerCase().includes(normalized))
    )).map((item) => ({
      resultId: `conversation:${item.conversationId}`,
      kind: "conversation" as const,
      title: item.title,
      snippet: item.messages.find((message) => message.role === "user")?.content ?? "Conversation",
      status: "available" as const,
      documentId: null,
      pageId: null,
      conversationId: item.conversationId,
      messageId: item.messages.find((message) => (
        message.role === "user" && message.content.toLowerCase().includes(normalized)
      ))?.messageId ?? null,
    }))
    return { query, results: conversations }
  }

  async getConversation(conversationId: string): Promise<DesktopConversation> {
    const conversation = requireConversation(this.conversationResults, conversationId)
    this.lastConversationId = conversationId
    return conversation
  }

  async createConversation(title: string | undefined, requestId: string): Promise<DesktopConversation> {
    const now = new Date().toISOString()
    const conversation: DesktopConversation = {
      conversationId: `conversation-${requestId}`,
      title: title?.trim() || "New conversation",
      draftText: "",
      createdAt: now,
      updatedAt: now,
      messages: [],
    }
    this.conversationResults = [conversation, ...this.conversationResults]
    this.lastConversationId = conversation.conversationId
    return conversation
  }

  async renameConversation(conversationId: string, title: string, requestId: string): Promise<DesktopConversation> {
    void requestId
    return this.updateConversation(conversationId, (conversation) => ({ ...conversation, title: title.trim(), updatedAt: new Date().toISOString() }))
  }

  async deleteConversation(conversationId: string, requestId: string): Promise<DesktopConversationList> {
    void requestId
    requireConversation(this.conversationResults, conversationId)
    this.conversationResults = this.conversationResults.filter((item) => item.conversationId !== conversationId)
    if (this.lastConversationId === conversationId) this.lastConversationId = this.conversationResults[0]?.conversationId ?? null
    return this.conversations()
  }

  async saveConversationDraft(conversationId: string, draftText: string, requestId: string): Promise<DesktopConversation> {
    void requestId
    return this.updateConversation(conversationId, (conversation) => ({ ...conversation, draftText }))
  }

  async askConversation(conversationId: string, question: string, requestId: string): Promise<DesktopConversation> {
    const conversation = requireConversation(this.conversationResults, conversationId)
    const now = new Date().toISOString()
    const userMessageId = `user-${requestId}`
    const assistantMessageId = `assistant-${requestId}`
    const source = this.importJobResults.find((task) => task.document?.availability === "available")?.document
    const citations = source ? [{
      evidenceId: `evidence-${source.documentId}`,
      documentId: source.documentId,
      documentName: source.name,
      section: "Document",
      locator: { ordinal: 0 },
      excerpt: `Original content for ${source.name}.`,
      channels: ["fts"],
      sourceAvailable: true,
    }] : []
    const answerText = citations.length ? `## Answer\n\n${citations[0].excerpt} [1]` : `No available source evidence was found for: ${question}`
    emitBridgeEvent(this.listeners, { sequence: Date.now(), kind: "answer.delta", data: { requestId, answerId: assistantMessageId, delta: answerText, replace: true, attempt: 1 } })
    const answerVersionId = `version-${requestId}`
    const next: DesktopConversation = {
      ...conversation,
      title: conversation.messages.length ? conversation.title : question.slice(0, 60),
      draftText: "",
      updatedAt: now,
      messages: [...conversation.messages, {
        messageId: userMessageId,
        ordinal: conversation.messages.length,
        role: "user",
        content: question,
        status: "completed",
        selectedAnswerVersionId: null,
        createdAt: now,
        updatedAt: now,
        answerVersions: [],
      }, {
        messageId: assistantMessageId,
        ordinal: conversation.messages.length + 1,
        role: "assistant",
        content: "",
        status: "completed",
        selectedAnswerVersionId: answerVersionId,
        createdAt: now,
        updatedAt: now,
        answerVersions: [{
          answerVersionId,
          versionNumber: 1,
          answerText,
          retrievalPlan: { query: question, terms: question.split(/\s+/), source: "deterministic" },
          citations,
          sourceImages: [],
          degradations: [],
          status: "completed",
          interruptionCode: null,
          interruptionReason: null,
          createdAt: now,
        }],
      }],
    }
    return this.replaceConversation(next)
  }

  async regenerateConversationAnswer(conversationId: string, assistantMessageId: string, requestId: string): Promise<DesktopConversation> {
    const conversation = requireConversation(this.conversationResults, conversationId)
    const message = conversation.messages.find((item) => item.messageId === assistantMessageId && item.role === "assistant")
    if (!message) throw new Error("The assistant message was not found.")
    const selected = message.answerVersions.find((version) => version.answerVersionId === message.selectedAnswerVersionId)
    if (!selected) throw new Error("This answer cannot be regenerated.")
    const now = new Date().toISOString()
    const answerVersionId = `version-${requestId}`
    const answerText = `${selected.answerText}\n\n_Regenerated_`
    emitBridgeEvent(this.listeners, { sequence: Date.now(), kind: "answer.delta", data: { requestId, answerId: assistantMessageId, delta: answerText, replace: true, attempt: 1 } })
    return this.updateConversation(conversationId, (current) => ({
      ...current,
      updatedAt: now,
      messages: current.messages.map((item) => item.messageId === assistantMessageId ? {
        ...item,
        status: "completed",
        selectedAnswerVersionId: answerVersionId,
        answerVersions: [...item.answerVersions, { ...selected, answerVersionId, versionNumber: item.answerVersions.length + 1, answerText, createdAt: now }],
      } : item),
    }))
  }

  async selectAnswerVersion(conversationId: string, assistantMessageId: string, answerVersionId: string, requestId: string): Promise<DesktopConversation> {
    void requestId
    return this.updateConversation(conversationId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => message.messageId === assistantMessageId ? { ...message, selectedAnswerVersionId: answerVersionId } : message),
    }))
  }

  async knowledgePages(): Promise<DesktopKnowledgePages> {
    return this.knowledgePagesStore.list()
  }

  async getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage> {
    return this.knowledgePagesStore.get(pageId)
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
    return this.knowledgePagesStore.saveDraft(pageId, kind, title, contentMarkdown, requestId)
  }

  async publishKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void requestId
    return this.knowledgePagesStore.publish(pageId)
  }

  async searchKnowledgeSources(query: string): Promise<DesktopKnowledgeSourceCandidate[]> {
    return this.knowledgePagesStore.searchSources(query)
  }

  async bindKnowledgePageSource(
    pageId: string,
    claimText: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    void requestId
    return this.knowledgePagesStore.bindSource(pageId, claimText, evidenceId)
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
    this.importJobResults = updateImportTasks(this.importJobResults, jobId, "paused")
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
    this.importJobResults = updateImportTasks(this.importJobResults, jobId, "cancelled")
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

  private replaceConversation(conversation: DesktopConversation): DesktopConversation {
    this.conversationResults = [
      conversation,
      ...this.conversationResults.filter((item) => item.conversationId !== conversation.conversationId),
    ]
    this.lastConversationId = conversation.conversationId
    return conversation
  }

  private updateConversation(
    conversationId: string,
    update: (conversation: DesktopConversation) => DesktopConversation,
  ): DesktopConversation {
    return this.replaceConversation(update(requireConversation(this.conversationResults, conversationId)))
  }

  private activate(kbDir: string, name: string): DesktopKnowledgeBaseActivation {
    const previousKbDir = this.activeKnowledgeBaseResult?.kbDir ?? null
    const checkpointed = previousKbDir !== null && previousKbDir !== kbDir
    this.activeKnowledgeBaseResult = {
      kbDir,
      name,
      schemaVersion: 20,
      lastCheckpointAt: checkpointed ? new Date().toISOString() : null,
    }
    this.importJobResults = []
    this.groundedAnswerResults = []
    this.conversationResults = []
    this.lastConversationId = null
    this.knowledgePagesStore.reset()
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

}
