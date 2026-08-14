import { Channel, invoke } from "@tauri-apps/api/core"
import {
  DesktopBridgeError,
  type DesktopBridge,
  type DesktopBridgeEvent,
  type DesktopBridgeHandshake,
  type DesktopCancelResult,
  type DesktopEngineHealth,
  type DesktopKnowledgeBaseInspection,
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
