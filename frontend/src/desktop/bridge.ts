import { Channel, invoke } from "@tauri-apps/api/core"
import {
  DesktopBridgeError,
  type DesktopBridge,
  type DesktopBridgeEvent,
  type DesktopActiveKnowledgeBase,
  type DesktopBridgeHandshake,
  type DesktopDiagnosticBundle,
  type DesktopCancelResult,
  type DesktopEngineHealth,
  type DesktopGroundedAnswer,
  type DesktopGroundedAnswers,
  type DesktopConversation,
  type DesktopConversationList,
  type DesktopImportControlResult,
  type DesktopImportDropEvent,
  type DesktopRuntimeEvent,
  type DesktopRuntimeLaunchIntent,
  type DesktopImportSourceInspection,
  type DesktopImportSourcePicker,
  type DesktopKnowledgeBaseActivation,
  type DesktopGlobalSearchResults,
  type DesktopModelSettings,
  type DesktopModelConnectionTest,
  type DesktopKnowledgePage,
  type DesktopKnowledgePages,
  type DesktopKnowledgePageKind,
  type DesktopDocumentVersionCandidate,
  type DesktopDocumentVersionCandidates,
  type DesktopDocumentVersionCandidateDecision,
  type DesktopKnowledgeReconciliationConflicts,
  type DesktopKnowledgeReconciliationCommit,
  type DesktopKnowledgeReconciliationDecision,
  type DesktopImportJobs,
  type DesktopRawDocument,
  type DesktopRecoveryOverride,
  type DesktopTextDocumentImport,
} from "./contracts"
import {
  conversation,
  conversationList,
  globalSearchResults,
  nextSubscriptionId,
  runtimeLaunchIntents,
  toDesktopBridgeError,
} from "./bridge-normalizers"

type DesktopWindow = Window & {
  __OPENKB_DESKTOP__?: unknown
  __TAURI_INTERNALS__?: unknown
}

/** True only when the Desktop Workbench is running inside its Tauri shell. */
export function isDesktopShell(): boolean {
  if (typeof window === "undefined") return false
  const desktopWindow = window as DesktopWindow
  return Boolean(desktopWindow.__OPENKB_DESKTOP__ ?? desktopWindow.__TAURI_INTERNALS__)
}

/** Production Bridge: the sole React caller of Tauri commands and channels. */
export class TauriDesktopBridge implements DesktopBridge {
  async handshake(): Promise<DesktopBridgeHandshake> {
    return this.call<DesktopBridgeHandshake>("desktop_bridge_handshake")
  }

  async health(): Promise<DesktopEngineHealth> {
    return this.call<DesktopEngineHealth>("desktop_engine_health")
  }

  async createKnowledgeBase(
    kbDir: string,
    name: string | undefined,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseActivation> {
    return this.call<DesktopKnowledgeBaseActivation>("desktop_create_knowledge_base", {
      kbDir,
      name,
      requestId,
    })
  }

  async openKnowledgeBase(
    kbDir: string,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseActivation> {
    return this.call<DesktopKnowledgeBaseActivation>("desktop_open_knowledge_base", {
      kbDir,
      requestId,
    })
  }

  async activeKnowledgeBase(): Promise<DesktopActiveKnowledgeBase> {
    return this.call<DesktopActiveKnowledgeBase>("desktop_active_knowledge_base")
  }

  async chooseKnowledgeBaseDirectory(): Promise<string | null> {
    const { open } = await import("@tauri-apps/plugin-dialog")
    const selection = await open({ directory: true, multiple: false })
    return typeof selection === "string" ? selection : null
  }

  async revealKnowledgeBaseDirectory(kbDir: string): Promise<void> {
    return this.call<void>("desktop_reveal_knowledge_base_directory", { kbDir })
  }

  async revealApplicationLogDirectory(): Promise<void> {
    return this.call<void>("desktop_reveal_application_log_directory")
  }

  async modelSettings(): Promise<DesktopModelSettings> {
    return this.call<DesktopModelSettings>("desktop_model_settings")
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
    return this.call<DesktopModelSettings>("desktop_save_model_settings", {
      provider,
      model,
      apiBaseUrl,
      apiKey,
      maxConcurrentModelCalls,
      initialTimeoutSeconds,
      requestId,
    })
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
    const result = await this.call<Record<string, unknown>>("desktop_test_model_connection", {
      provider,
      model,
      apiBaseUrl,
      apiKey,
      maxConcurrentModelCalls,
      initialTimeoutSeconds,
      requestId,
    })
    return {
      ok: Boolean(result.ok),
      model: typeof result.model === "string" ? result.model : model,
      latencyMs: typeof result.latency_ms === "number" ? result.latency_ms : 0,
      attemptCount: typeof result.attempt_count === "number" ? result.attempt_count : 0,
    }
  }

  async exportDiagnosticBundle(
    destination: string,
    requestId: string,
  ): Promise<DesktopDiagnosticBundle> {
    return this.call<DesktopDiagnosticBundle>("desktop_export_diagnostic_bundle", {
      destination,
      requestId,
    })
  }

  async inspectImportSources(
    sourcePaths: string[],
    requestId: string,
  ): Promise<DesktopImportSourceInspection> {
    return this.call<DesktopImportSourceInspection>("desktop_inspect_import_sources", {
      sourcePaths,
      requestId,
    })
  }

  async chooseImportSources(picker: DesktopImportSourcePicker): Promise<string[]> {
    const { open } = await import("@tauri-apps/plugin-dialog")
    const selection = await open({
      directory: picker === "directory",
      multiple: picker === "files",
    })
    if (selection === null) return []
    return Array.isArray(selection) ? selection : [selection]
  }

  async subscribeImportDrops(
    listener: (event: DesktopImportDropEvent) => void,
  ): Promise<() => void> {
    const { getCurrentWindow } = await import("@tauri-apps/api/window")
    return getCurrentWindow().onDragDropEvent((event) => {
      const payload = event.payload
      listener({
        type: payload.type,
        paths: "paths" in payload ? payload.paths : [],
      })
    })
  }

  async takeLaunchIntents(): Promise<DesktopRuntimeLaunchIntent[]> {
    return runtimeLaunchIntents(await this.call<unknown>("desktop_take_launch_intents"))
  }

  async subscribeRuntimeEvents(
    listener: (event: DesktopRuntimeEvent) => void,
  ): Promise<() => void> {
    const { listen } = await import("@tauri-apps/api/event")
    const listeners = await Promise.all([
      listen("desktop://launch-intents-ready", () => listener({ kind: "launch_intents_available" })),
      listen("desktop://task-center", () => listener({ kind: "tasks.requested" })),
      listen("desktop://engine-restarted", () => listener({ kind: "engine.restarted" })),
      listen("desktop://active-knowledge-base-restored", () => listener({ kind: "activeKnowledgeBaseRestored" })),
      listen("desktop://tray-restored", () => listener({ kind: "tray.restored" })),
    ])
    return () => listeners.forEach((remove) => remove())
  }

  async readRawDocument(
    documentId: string,
    requestId: string,
    page = 0,
    focusLocator: Record<string, unknown> | undefined = undefined,
  ): Promise<DesktopRawDocument> {
    return this.call<DesktopRawDocument>("desktop_read_raw_document", {
      documentId,
      requestId,
      page,
      focusLocator,
    })
  }

  async importTextDocument(
    sourcePath: string,
    requestId: string,
  ): Promise<DesktopTextDocumentImport> {
    return this.call<DesktopTextDocumentImport>("desktop_import_text_document", {
      sourcePath,
      requestId,
    })
  }

  async importJobs(): Promise<DesktopImportJobs> {
    return this.call<DesktopImportJobs>("desktop_import_jobs")
  }

  async askGrounded(question: string, requestId: string): Promise<DesktopGroundedAnswer> {
    return this.call<DesktopGroundedAnswer>("desktop_ask_grounded", { question, requestId })
  }

  async retryInterruptedAnswer(
    answerId: string,
    requestId: string,
  ): Promise<DesktopGroundedAnswer> {
    return this.call<DesktopGroundedAnswer>("desktop_retry_interrupted_answer", {
      answerId,
      requestId,
    })
  }

  async groundedAnswers(): Promise<DesktopGroundedAnswers> {
    return this.call<DesktopGroundedAnswers>("desktop_grounded_answers")
  }

  async conversations(search = ""): Promise<DesktopConversationList> {
    return conversationList(await this.call<unknown>("desktop_conversations", { search }))
  }

  async globalSearch(query: string): Promise<DesktopGlobalSearchResults> {
    return globalSearchResults(
      await this.call<unknown>("desktop_global_search", { query }),
      query,
    )
  }

  async getConversation(conversationId: string): Promise<DesktopConversation> {
    return conversation(await this.call<unknown>("desktop_conversation", { conversationId }))
  }

  async createConversation(title: string | undefined, requestId: string): Promise<DesktopConversation> {
    return conversation(await this.call<unknown>("desktop_create_conversation", { title, requestId }))
  }

  async renameConversation(conversationId: string, title: string, requestId: string): Promise<DesktopConversation> {
    return conversation(await this.call<unknown>("desktop_rename_conversation", { conversationId, title, requestId }))
  }

  async deleteConversation(conversationId: string, requestId: string): Promise<DesktopConversationList> {
    return conversationList(await this.call<unknown>("desktop_delete_conversation", { conversationId, requestId }))
  }

  async saveConversationDraft(conversationId: string, draftText: string, requestId: string): Promise<DesktopConversation> {
    return conversation(await this.call<unknown>("desktop_save_conversation_draft", { conversationId, draftText, requestId }))
  }

  async askConversation(conversationId: string, question: string, requestId: string): Promise<DesktopConversation> {
    return conversation(await this.call<unknown>("desktop_ask_conversation", { conversationId, question, requestId }))
  }

  async regenerateConversationAnswer(conversationId: string, assistantMessageId: string, requestId: string): Promise<DesktopConversation> {
    return conversation(await this.call<unknown>("desktop_regenerate_conversation_answer", { conversationId, assistantMessageId, requestId }))
  }

  async selectAnswerVersion(conversationId: string, assistantMessageId: string, answerVersionId: string, requestId: string): Promise<DesktopConversation> {
    return conversation(await this.call<unknown>("desktop_select_answer_version", { conversationId, assistantMessageId, answerVersionId, requestId }))
  }

  async knowledgePages(): Promise<DesktopKnowledgePages> {
    return this.call<DesktopKnowledgePages>("desktop_knowledge_pages")
  }

  async getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_get_knowledge_page", { pageId })
  }

  async saveKnowledgePage(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_save_knowledge_page", {
      pageId,
      kind,
      title,
      contentMarkdown,
      requestId,
    })
  }

  async publishKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_publish_knowledge_page", { pageId, requestId })
  }

  async documentVersionCandidates(): Promise<DesktopDocumentVersionCandidates> {
    return this.call<DesktopDocumentVersionCandidates>("desktop_document_version_candidates")
  }

  async resolveDocumentVersionCandidate(
    candidateId: string,
    decision: DesktopDocumentVersionCandidateDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCandidate> {
    return this.call<DesktopDocumentVersionCandidate>("desktop_resolve_document_version_candidate", {
      candidateId,
      decision,
      requestId,
    })
  }

  async knowledgeReconciliationConflicts(): Promise<DesktopKnowledgeReconciliationConflicts> {
    return this.call<DesktopKnowledgeReconciliationConflicts>(
      "desktop_knowledge_reconciliation_conflicts",
    )
  }

  async stageKnowledgeReconciliationDecisions(
    candidateIds: string[],
    decision: DesktopKnowledgeReconciliationDecision | null,
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationConflicts> {
    return this.call<DesktopKnowledgeReconciliationConflicts>(
      "desktop_stage_knowledge_reconciliation_decisions",
      { candidateIds, decision, requestId },
    )
  }

  async commitKnowledgeReconciliationDecisions(
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationCommit> {
    return this.call<DesktopKnowledgeReconciliationCommit>(
      "desktop_commit_knowledge_reconciliation_decisions",
      { requestId },
    )
  }

  async pauseImportJob(jobId: string): Promise<DesktopImportControlResult> {
    return this.call<DesktopImportControlResult>("desktop_pause_import_job", { jobId })
  }

  async resumeImportJob(
    jobId: string,
    requestId: string,
  ): Promise<DesktopTextDocumentImport> {
    return this.call<DesktopTextDocumentImport>("desktop_resume_import_job", { jobId, requestId })
  }

  async recoverImportJob(
    jobId: string,
    recoveryOverride: DesktopRecoveryOverride,
    requestId: string,
  ): Promise<DesktopTextDocumentImport> {
    return this.call<DesktopTextDocumentImport>("desktop_recover_import_job", {
      jobId,
      recoveryOverride,
      requestId,
    })
  }

  async cancelImportJob(jobId: string): Promise<DesktopImportControlResult> {
    return this.call<DesktopImportControlResult>("desktop_cancel_import_job", { jobId })
  }

  async cancel(targetRequestId: string): Promise<DesktopCancelResult> {
    return this.call<DesktopCancelResult>("desktop_cancel", { targetRequestId })
  }

  async subscribe(listener: (event: DesktopBridgeEvent) => void): Promise<() => void> {
    const subscriptionId = nextSubscriptionId()
    const eventChannel = new Channel<DesktopBridgeEvent>()
    eventChannel.onmessage = listener
    await this.call<void>("desktop_subscribe", { subscriptionId, eventChannel })
    return () => {
      void this.call<void>("desktop_unsubscribe", { subscriptionId })
    }
  }

  private async call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
    try {
      return await invoke<T>(command, args)
    } catch (error) {
      throw toDesktopBridgeError(error)
    }
  }
}

class UnavailableDesktopBridge implements DesktopBridge {
  private unavailable(): Promise<never> {
    return Promise.reject(
      new DesktopBridgeError(
        "desktop_shell_unavailable",
        "The Desktop Bridge is only available inside OpenKB Desktop.",
      ),
    )
  }

  handshake(): Promise<DesktopBridgeHandshake> {
    return this.unavailable()
  }

  health(): Promise<DesktopEngineHealth> {
    return this.unavailable()
  }

  createKnowledgeBase(
    kbDir: string,
    name: string | undefined,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseActivation> {
    void kbDir
    void name
    void requestId
    return this.unavailable()
  }

  openKnowledgeBase(
    kbDir: string,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseActivation> {
    void kbDir
    void requestId
    return this.unavailable()
  }

  activeKnowledgeBase(): Promise<DesktopActiveKnowledgeBase> {
    return this.unavailable()
  }

  chooseKnowledgeBaseDirectory(): Promise<string | null> {
    return this.unavailable()
  }

  revealKnowledgeBaseDirectory(kbDir: string): Promise<void> {
    void kbDir
    return this.unavailable()
  }

  revealApplicationLogDirectory(): Promise<void> {
    return this.unavailable()
  }

  modelSettings(): Promise<DesktopModelSettings> {
    return this.unavailable()
  }

  saveModelSettings(
    provider: string,
    model: string,
    apiBaseUrl: string,
    apiKey: string,
    maxConcurrentModelCalls: number,
    initialTimeoutSeconds: number,
    requestId: string,
  ): Promise<DesktopModelSettings> {
    void provider
    void model
    void apiBaseUrl
    void apiKey
    void maxConcurrentModelCalls
    void initialTimeoutSeconds
    void requestId
    return this.unavailable()
  }

  testModelConnection(
    provider: string,
    model: string,
    apiBaseUrl: string,
    apiKey: string,
    maxConcurrentModelCalls: number,
    initialTimeoutSeconds: number,
    requestId: string,
  ): Promise<DesktopModelConnectionTest> {
    void provider
    void model
    void apiBaseUrl
    void apiKey
    void maxConcurrentModelCalls
    void initialTimeoutSeconds
    void requestId
    return this.unavailable()
  }

  exportDiagnosticBundle(
    destination: string,
    requestId: string,
  ): Promise<DesktopDiagnosticBundle> {
    void destination
    void requestId
    return this.unavailable()
  }

  inspectImportSources(
    sourcePaths: string[],
    requestId: string,
  ): Promise<DesktopImportSourceInspection> {
    void sourcePaths
    void requestId
    return this.unavailable()
  }

  chooseImportSources(picker: DesktopImportSourcePicker): Promise<string[]> {
    void picker
    return this.unavailable()
  }

  subscribeImportDrops(
    listener: (event: DesktopImportDropEvent) => void,
  ): Promise<() => void> {
    void listener
    return this.unavailable()
  }

  subscribeRuntimeEvents(
    listener: (event: DesktopRuntimeEvent) => void,
  ): Promise<() => void> {
    void listener
    return Promise.resolve(() => undefined)
  }

  takeLaunchIntents(): Promise<DesktopRuntimeLaunchIntent[]> {
    return Promise.resolve([])
  }

  readRawDocument(
    documentId: string,
    requestId: string,
    page?: number,
    focusLocator?: Record<string, unknown>,
  ): Promise<DesktopRawDocument> {
    void documentId
    void requestId
    void page
    void focusLocator
    return this.unavailable()
  }

  importTextDocument(sourcePath: string, requestId: string): Promise<DesktopTextDocumentImport> {
    void sourcePath
    void requestId
    return this.unavailable()
  }

  importJobs(): Promise<DesktopImportJobs> {
    return this.unavailable()
  }

  askGrounded(question: string, requestId: string): Promise<DesktopGroundedAnswer> {
    void question
    void requestId
    return this.unavailable()
  }

  retryInterruptedAnswer(answerId: string, requestId: string): Promise<DesktopGroundedAnswer> {
    void answerId
    void requestId
    return this.unavailable()
  }

  groundedAnswers(): Promise<DesktopGroundedAnswers> {
    return this.unavailable()
  }

  conversations(search?: string): Promise<DesktopConversationList> {
    void search
    return this.unavailable()
  }

  globalSearch(query: string): Promise<DesktopGlobalSearchResults> {
    void query
    return this.unavailable()
  }

  getConversation(conversationId: string): Promise<DesktopConversation> {
    void conversationId
    return this.unavailable()
  }

  createConversation(title: string | undefined, requestId: string): Promise<DesktopConversation> {
    void title
    void requestId
    return this.unavailable()
  }

  renameConversation(conversationId: string, title: string, requestId: string): Promise<DesktopConversation> {
    void conversationId
    void title
    void requestId
    return this.unavailable()
  }

  deleteConversation(conversationId: string, requestId: string): Promise<DesktopConversationList> {
    void conversationId
    void requestId
    return this.unavailable()
  }

  saveConversationDraft(conversationId: string, draftText: string, requestId: string): Promise<DesktopConversation> {
    void conversationId
    void draftText
    void requestId
    return this.unavailable()
  }

  askConversation(conversationId: string, question: string, requestId: string): Promise<DesktopConversation> {
    void conversationId
    void question
    void requestId
    return this.unavailable()
  }

  regenerateConversationAnswer(conversationId: string, assistantMessageId: string, requestId: string): Promise<DesktopConversation> {
    void conversationId
    void assistantMessageId
    void requestId
    return this.unavailable()
  }

  selectAnswerVersion(conversationId: string, assistantMessageId: string, answerVersionId: string, requestId: string): Promise<DesktopConversation> {
    void conversationId
    void assistantMessageId
    void answerVersionId
    void requestId
    return this.unavailable()
  }

  knowledgePages(): Promise<DesktopKnowledgePages> {
    return this.unavailable()
  }

  getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage> {
    void pageId
    return this.unavailable()
  }

  saveKnowledgePage(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    void pageId
    void kind
    void title
    void contentMarkdown
    void requestId
    return this.unavailable()
  }

  publishKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void pageId
    void requestId
    return this.unavailable()
  }

  documentVersionCandidates(): Promise<DesktopDocumentVersionCandidates> {
    return this.unavailable()
  }

  resolveDocumentVersionCandidate(
    candidateId: string,
    decision: DesktopDocumentVersionCandidateDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCandidate> {
    void candidateId
    void decision
    void requestId
    return this.unavailable()
  }

  knowledgeReconciliationConflicts(): Promise<DesktopKnowledgeReconciliationConflicts> {
    return this.unavailable()
  }

  stageKnowledgeReconciliationDecisions(
    candidateIds: string[],
    decision: DesktopKnowledgeReconciliationDecision | null,
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationConflicts> {
    void candidateIds
    void decision
    void requestId
    return this.unavailable()
  }

  commitKnowledgeReconciliationDecisions(
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationCommit> {
    void requestId
    return this.unavailable()
  }

  pauseImportJob(jobId: string): Promise<DesktopImportControlResult> {
    void jobId
    return this.unavailable()
  }

  resumeImportJob(jobId: string, requestId: string): Promise<DesktopTextDocumentImport> {
    void jobId
    void requestId
    return this.unavailable()
  }

  recoverImportJob(
    jobId: string,
    recoveryOverride: DesktopRecoveryOverride,
    requestId: string,
  ): Promise<DesktopTextDocumentImport> {
    void jobId
    void recoveryOverride
    void requestId
    return this.unavailable()
  }

  cancelImportJob(jobId: string): Promise<DesktopImportControlResult> {
    void jobId
    return this.unavailable()
  }

  cancel(targetRequestId: string): Promise<DesktopCancelResult> {
    void targetRequestId
    return this.unavailable()
  }

  subscribe(listener: (event: DesktopBridgeEvent) => void): Promise<() => void> {
    void listener
    return this.unavailable()
  }
}

export function createDesktopBridge(): DesktopBridge {
  return isDesktopShell() ? new TauriDesktopBridge() : new UnavailableDesktopBridge()
}
