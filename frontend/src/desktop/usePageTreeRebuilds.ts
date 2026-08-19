import { useEffect, useState } from "react"
import type {
  DesktopBridge,
  DesktopPageTreeEnrichmentTask,
  DesktopPageTreeRebuildTask,
} from "./contracts"

interface RebuildState {
  kbDir: string | null
  tasks: DesktopPageTreeRebuildTask[]
  enrichments: DesktopPageTreeEnrichmentTask[]
  error: string | null
}

/** Polls persisted PageTree work only while the global Task Center is visible. */
export function usePageTreeRebuilds({
  bridge,
  kbDir,
  open,
  engineReady,
}: {
  bridge: DesktopBridge
  kbDir: string | null
  open: boolean
  engineReady: boolean
}) {
  const [state, setState] = useState<RebuildState>({
    kbDir: null,
    tasks: [],
    enrichments: [],
    error: null,
  })

  useEffect(() => {
    if (!open || !kbDir || !engineReady) return
    let disposed = false
    let timer: number | undefined
    const refresh = async () => {
      try {
        const result = await bridge.importJobs()
        if (disposed) return
        setState({
          kbDir,
          tasks: result.pageTreeRebuilds,
          enrichments: result.pageTreeEnrichments,
          error: null,
        })
      } catch (reason) {
        if (!disposed) {
          setState({
            kbDir,
            tasks: [],
            enrichments: [],
            error: reason instanceof Error ? reason.message : String(reason),
          })
        }
      }
      if (!disposed) timer = window.setTimeout(refresh, 1_000)
    }
    void refresh()
    return () => {
      disposed = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [bridge, engineReady, kbDir, open])

  return engineReady && state.kbDir === kbDir
    ? state
    : { kbDir, tasks: [], enrichments: [], error: null }
}
