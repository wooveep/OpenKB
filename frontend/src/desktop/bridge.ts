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
  type DesktopImportControlResult,
  type DesktopImportDropEvent,
  type DesktopRuntimeEvent,
  type DesktopRuntimeLaunchIntent,
  type DesktopImportSourceInspection,
  type DesktopImportSourcePicker,
  type DesktopKnowledgeBaseActivation,
  type DesktopKnowledgeBaseInspection,
  type DesktopModelSettings,
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

let subscriptionSequence = 0

type DesktopWindow = Window & {
  __OPENKB_DESKTOP__?: unknown
  __TAURI_INTERNALS__?: unknown
}

/** True only in Tauri; browser mode keeps its existing REST compatibility path. */
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

  async inspectKnowledgeBase(
    kbDir: string,
    requestId: string,
  ): Promise<DesktopKnowledgeBaseInspection> {
    return this.call<DesktopKnowledgeBaseInspection>("desktop_inspect_knowledge_base", {
      kbDir,
      requestId,
    })
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

  async modelSettings(): Promise<DesktopModelSettings> {
    return this.call<DesktopModelSettings>("desktop_model_settings")
  }

  async saveModelSettings(
    model: string,
    credentialReference: string,
    maxConcurrentModelCalls: number,
    initialTimeoutSeconds: number,
    requestId: string,
  ): Promise<DesktopModelSettings> {
    return this.call<DesktopModelSettings>("desktop_save_model_settings", {
      model,
      credentialReference,
      maxConcurrentModelCalls,
      initialTimeoutSeconds,
      requestId,
    })
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

  inspectKnowledgeBase(kbDir: string, requestId: string): Promise<DesktopKnowledgeBaseInspection> {
    void kbDir
    void requestId
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

  modelSettings(): Promise<DesktopModelSettings> {
    return this.unavailable()
  }

  saveModelSettings(
    model: string,
    credentialReference: string,
    maxConcurrentModelCalls: number,
    initialTimeoutSeconds: number,
    requestId: string,
  ): Promise<DesktopModelSettings> {
    void model
    void credentialReference
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

function nextSubscriptionId(): string {
  subscriptionSequence += 1
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `desktop-subscription-${Date.now()}-${subscriptionSequence}`
}

function toDesktopBridgeError(error: unknown): DesktopBridgeError {
  if (error instanceof DesktopBridgeError) return error
  if (error && typeof error === "object") {
    const candidate = error as { code?: unknown; message?: unknown }
    if (typeof candidate.code === "string" && typeof candidate.message === "string") {
      return new DesktopBridgeError(candidate.code, candidate.message)
    }
  }
  return new DesktopBridgeError(
    "desktop_bridge_failed",
    error instanceof Error ? error.message : String(error),
  )
}

function runtimeLaunchIntents(payload: unknown): DesktopRuntimeLaunchIntent[] {
  if (!Array.isArray(payload)) return []
  return payload.filter(isRecord).flatMap((intent) => {
    const normalized = runtimeLaunchIntent(intent)
    return normalized === null ? [] : [normalized]
  })
}

function runtimeLaunchIntent(payload: Record<string, unknown>): DesktopRuntimeLaunchIntent | null {
  if (payload.kind === "openKnowledgeBase" && nonEmptyString(payload.kbDir)) {
    return { kind: "openKnowledgeBase", kbDir: payload.kbDir }
  }
  if (payload.kind === "importSources" && Array.isArray(payload.sourcePaths)) {
    const sourcePaths = payload.sourcePaths.filter(nonEmptyString)
    return sourcePaths.length ? { kind: "importSources", sourcePaths } : null
  }
  return null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0
}
