import { Channel, invoke } from "@tauri-apps/api/core"
import {
  DesktopBridgeError,
  type DesktopBridge,
  type DesktopBridgeEvent,
  type DesktopActiveKnowledgeBase,
  type DesktopBridgeHandshake,
  type DesktopCancelResult,
  type DesktopEngineHealth,
  type DesktopImportControlResult,
  type DesktopKnowledgeBaseActivation,
  type DesktopKnowledgeBaseInspection,
  type DesktopImportJobs,
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

  importTextDocument(sourcePath: string, requestId: string): Promise<DesktopTextDocumentImport> {
    void sourcePath
    void requestId
    return this.unavailable()
  }

  importJobs(): Promise<DesktopImportJobs> {
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
