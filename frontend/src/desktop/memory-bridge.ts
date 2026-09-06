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
  DesktopImportJobs,
  DesktopRawDocument,
  DesktopActiveKnowledgeBase,
  DesktopEngineHealth,
  DesktopGroundedAnswer,
  DesktopGroundedAnswers,
  DesktopConversation,
  DesktopConversationList,
  DesktopKnowledgeBase,
  DesktopKnowledgeBaseActivation,
  DesktopKnowledgeReanalysisOverview,
  DesktopKnowledgeReanalysisRun,
  DesktopGlobalSearchResults,
  DesktopPageTreeEnrichmentControlResult,
  DesktopKnowledgeGraphExtractionControlResult,
  DesktopDocumentVersionCandidate,
  DesktopDocumentVersionCandidates,
  DesktopDocumentVersionCandidateDecision,
  DesktopDocumentLineageDecision,
  DesktopDocumentVersionCatalog,
  DesktopDocumentVersionDiffs,
  DesktopVersionFilter,
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
import { MemoryDocumentVersionStore } from "./memory-document-version-store"
import { MemoryModelSettingsBridge } from "./memory-model-settings"

function emptyRetrievalTrace(canonicalEvidenceIds: string[] = []) {
  return {
    catalogGenerationIds: [],
    pageTreeGenerationIds: [],
    channels: [],
    triggerReasons: [],
    degradationReasons: [],
    selectedNodeIds: [],
    canonicalEvidenceIds,
    fusionPolicyVersion: "openkb.rrf-protected-baseline-routed.v2",
    navigationSnapshotIds: [],
    navigationRoutes: [],
    navigationReadCount: 0,
    sourceWindowCount: 0,
    linkHopCount: 0,
    pageTreeSupplementCount: 0,
    semanticStructureState: "unknown" as const,
    questionGoal: "",
    questionFacets: [],
    questionFacetPlanDigest: "",
    queryPlanningPromptContractDigest: "",
    queryPlanningExecutionProfileJson: "",
    queryPlanningExecutionProfileDigest: "",
    facetCoverage: [],
    coverageGateState: "unknown",
    navigationRoundCount: 0,
    navigationActionKinds: [],
    navigationStopReason: "",
    navigationModelCalls: 0,
    navigationLogicalReadCount: 0,
    navigationSourceTokens: 0,
    groundingInputBudgetTokens: 0,
    evidenceInputTokens: 0,
    guidanceInputTokens: 0,
    versionNavigationSnapshotId: "",
    versionCatalogRevisionId: "",
    versionCatalogDigest: "",
    versionScopeMode: "all_available",
    versionScopeStatus: "not_applicable",
    versionScopeLineageIds: [],
    versionScopeLabels: [],
    versionScopeDocumentIds: [],
    versionScopeSelectionReason: "memory_bridge",
    versionScopeDegradationReason: "",
  }
}

function emptyImportTelemetry() {
  return {
    importProgress: [],
    modelUsage: [],
    modelUsageAggregate: null,
    modelActivity: null,
    legacyModelRecovery: null,
  }
}

/** In-memory Bridge for React component tests; it never touches Tauri or Python. */
export class MemoryDesktopBridge extends MemoryModelSettingsBridge implements DesktopBridge {
  private readonly listeners = new Set<(event: DesktopBridgeEvent) => void>()
  private readonly handshakeResult: DesktopBridgeHandshake
  private readonly healthResult: DesktopEngineHealth
  private activeKnowledgeBaseResult: DesktopKnowledgeBase | null = null
  private importJobResults: DesktopImportTask[] = []
  private groundedAnswerResults: DesktopGroundedAnswer[] = []
  private conversationResults: DesktopConversation[] = []
  private lastConversationId: string | null = null
  private readonly documentVersions = new MemoryDocumentVersionStore()

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
    super()
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

  async exportDiagnosticBundle(
    destination: string,
    requestId: string,
  ): Promise<DesktopDiagnosticBundle> {
    void requestId
    return {
      path: destination,
      files: ["manifest.json", "model-settings.json", "import-jobs.json", "model-calls.json", "model-usage.json"],
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
    _parserMode?: "auto" | "fast" | "enhanced",
  ): Promise<DesktopTextDocumentImport> {
    void _parserMode
    if (this.activeKnowledgeBaseResult === null) {
      throw new Error("Open a Desktop Knowledge Base before importing a document.")
    }
    const name = sourceName(sourcePath) || "document.txt"
    const jobId = `job-${requestId}`
    const documentId = `document-${requestId}`
    const stages = ["preflight", "raw_asset", "document_ir", "evidence", "deterministic_page_tree", "model_analysis", "search"] as const
    const progress = [20, 35, 55, 75, 79, 85, 100]
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
      ...emptyImportTelemetry(),
    }
    this.importJobResults = [result, ...this.importJobResults]
    return result
  }

  async importJobs(): Promise<DesktopImportJobs> {
    return {
      jobs: this.importJobResults,
      pageTreeRebuilds: [],
      pageTreeEnrichments: [],
      knowledgeGraphExtractions: [],
      catalogRebuild: null,
    }
  }

  async knowledgeReanalysis(): Promise<DesktopKnowledgeReanalysisOverview> {
    return { documents: [], runs: [] }
  }

  async startKnowledgeReanalysis(
    documentIds: string[],
    requestId: string,
  ): Promise<DesktopKnowledgeReanalysisRun> {
    void documentIds
    void requestId
    throw new Error("Knowledge Reanalysis is not available in the renderer preview.")
  }

  async retryKnowledgeReanalysis(
    jobId: string,
    requestId: string,
  ): Promise<DesktopKnowledgeReanalysisRun> {
    void jobId
    void requestId
    throw new Error("Knowledge Reanalysis is not available in the renderer preview.")
  }

  async askGrounded(
    question: string,
    requestId: string,
    versionFilter?: DesktopVersionFilter,
  ): Promise<DesktopGroundedAnswer> {
    if (this.activeKnowledgeBaseResult === null) {
      throw new Error("Open a Desktop Knowledge Base before asking a question.")
    }
    const source = this.importJobResults.find((task) => (
      task.document?.availability === "available"
      && (!versionFilter?.documentIds.length
        || versionFilter.documentIds.includes(task.document.documentId))
    ))?.document
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
      retrievalTrace: emptyRetrievalTrace(citations.map((citation) => citation.evidenceId)),
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

  async askConversation(
    conversationId: string,
    question: string,
    requestId: string,
    versionFilter?: DesktopVersionFilter,
  ): Promise<DesktopConversation> {
    const conversation = requireConversation(this.conversationResults, conversationId)
    const now = new Date().toISOString()
    const userMessageId = `user-${requestId}`
    const assistantMessageId = `assistant-${requestId}`
    const source = this.importJobResults.find((task) => (
      task.document?.availability === "available"
      && (!versionFilter?.documentIds.length
        || versionFilter.documentIds.includes(task.document.documentId))
    ))?.document
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
          retrievalTrace: emptyRetrievalTrace(citations.map((citation) => citation.evidenceId)),
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

  async documentVersionCandidates(): Promise<DesktopDocumentVersionCandidates> {
    return this.documentVersions.pendingCandidates()
  }

  async documentVersionCatalog(): Promise<DesktopDocumentVersionCatalog> {
    return this.documentVersions.catalogSnapshot()
  }

  async confirmDocumentLineage(
    decision: DesktopDocumentLineageDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCatalog> {
    return this.documentVersions.confirmLineage(decision, requestId, this.importJobResults)
  }

  async documentVersionDiffs(lineageId: string): Promise<DesktopDocumentVersionDiffs> {
    return this.documentVersions.diffsForLineage(lineageId)
  }

  async resolveDocumentVersionCandidate(
    candidateId: string,
    decision: DesktopDocumentVersionCandidateDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCandidate> {
    void requestId
    return this.documentVersions.resolveCandidate(candidateId, decision)
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
        importProgress: task.importProgress,
        modelUsage: task.modelUsage,
        modelUsageAggregate: task.modelUsageAggregate,
        modelActivity: task.modelActivity,
        legacyModelRecovery: task.legacyModelRecovery,
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
      importProgress: task.importProgress,
      modelUsage: task.modelUsage,
      modelUsageAggregate: task.modelUsageAggregate,
      modelActivity: task.modelActivity,
      legacyModelRecovery: task.legacyModelRecovery,
    }
    this.importJobResults = [result, ...this.importJobResults.filter((item) => item.job.jobId !== jobId)]
    return result
  }

  async cancelImportJob(jobId: string): Promise<DesktopImportControlResult> {
    this.importJobResults = updateImportTasks(this.importJobResults, jobId, "cancelled")
    return { jobId, accepted: true }
  }

  async cancelPageTreeEnrichment(documentId: string): Promise<DesktopPageTreeEnrichmentControlResult> {
    return { documentId, accepted: false }
  }
  async retryPageTreeEnrichment(documentId: string): Promise<DesktopPageTreeEnrichmentControlResult> {
    return { documentId, accepted: false }
  }
  async cancelKnowledgeGraphExtraction(documentId: string): Promise<DesktopKnowledgeGraphExtractionControlResult> {
    return { documentId, accepted: false }
  }

  async retryKnowledgeGraphExtraction(documentId: string): Promise<DesktopKnowledgeGraphExtractionControlResult> {
    return { documentId, accepted: false }
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
      schemaVersion: 26,
      lastCheckpointAt: checkpointed ? new Date().toISOString() : null,
    }
    this.importJobResults = []
    this.groundedAnswerResults = []
    this.conversationResults = []
    this.lastConversationId = null
    this.documentVersions.reset()
    this.resetKnowledgePages()
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

  protected knowledgePagesAvailable(): boolean {
    return this.activeKnowledgeBaseResult !== null
  }

}
