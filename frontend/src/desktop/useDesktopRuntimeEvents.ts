import { useEffect, type MutableRefObject } from "react"
import type {
  DesktopBridge,
  DesktopRuntimeEvent,
  DesktopRuntimeLaunchIntent,
} from "./contracts"
import { nextDesktopRequestId } from "./request-id"

interface DesktopRuntimeEventOptions {
  bridge: DesktopBridge
  importSourcesRef: MutableRefObject<(sourcePaths: string[]) => void>
  refreshActiveKnowledgeBase: () => Promise<void>
  setLoading: (loading: boolean) => void
  setLoadError: (error: string | null) => void
  setSection: (section: "overview" | "documents") => void
}

/** Applies Shell lifecycle events through the typed Desktop Bridge. */
export function useDesktopRuntimeEvents({
  bridge,
  importSourcesRef,
  refreshActiveKnowledgeBase,
  setLoading,
  setLoadError,
  setSection,
}: DesktopRuntimeEventOptions): void {
  useEffect(() => {
    let disposed = false
    let unsubscribe: (() => void) | undefined
    let pending = Promise.resolve()
    const applyIntent = (intent: DesktopRuntimeLaunchIntent) => {
      if (intent.kind === "openKnowledgeBase") {
        return openKnowledgeBase(
          bridge,
          intent.kbDir,
          refreshActiveKnowledgeBase,
          setLoading,
          setLoadError,
          setSection,
        )
      }
      importSourcesRef.current(intent.sourcePaths)
      setSection("documents")
      return Promise.resolve()
    }
    const drainLaunchIntents = () => {
      pending = pending.then(async () => {
        for (const intent of await bridge.takeLaunchIntents()) await applyIntent(intent)
      }).catch(() => undefined)
    }
    const receive = (event: DesktopRuntimeEvent) => {
      if (event.kind === "launch_intents_available") {
        drainLaunchIntents()
      } else if (event.kind === "tasks.requested") {
        setSection("documents")
      } else {
        void refreshActiveKnowledgeBase()
      }
    }
    void bridge.subscribeRuntimeEvents(receive).then((remove) => {
      if (disposed) remove()
      else {
        unsubscribe = remove
        drainLaunchIntents()
      }
    }).catch(() => {
      // Browser-mode workbench previews use the Bridge's unavailable no-op.
    })
    return () => {
      disposed = true
      unsubscribe?.()
    }
  }, [bridge, importSourcesRef, refreshActiveKnowledgeBase, setLoading, setLoadError, setSection])
}

async function openKnowledgeBase(
  bridge: DesktopBridge,
  kbDir: string,
  refreshActiveKnowledgeBase: () => Promise<void>,
  setLoading: (loading: boolean) => void,
  setLoadError: (error: string | null) => void,
  setSection: (section: "overview" | "documents") => void,
): Promise<void> {
  setLoading(true)
  setLoadError(null)
  try {
    await bridge.openKnowledgeBase(kbDir, nextDesktopRequestId("desktop-launch"))
    await refreshActiveKnowledgeBase()
    setSection("overview")
  } catch (error) {
    await refreshActiveKnowledgeBase()
    setLoadError(error instanceof Error ? error.message : String(error))
  }
}
