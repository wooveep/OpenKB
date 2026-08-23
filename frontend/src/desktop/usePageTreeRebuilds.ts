import { useEffect, useState } from "react"
import type {
  DesktopBridge,
  DesktopCatalogRebuildTask,
  DesktopKnowledgeGraphExtractionTask,
  DesktopPageTreeEnrichmentTask,
  DesktopPageTreeRebuildTask,
} from "./contracts"

interface RebuildState {
  kbDir: string | null
  tasks: DesktopPageTreeRebuildTask[]
  enrichments: DesktopPageTreeEnrichmentTask[]
  graphExtractions: DesktopKnowledgeGraphExtractionTask[]
  catalog: DesktopCatalogRebuildTask | null
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
    graphExtractions: [],
    catalog: null,
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
          graphExtractions: result.knowledgeGraphExtractions,
          catalog: result.catalogRebuild,
          error: null,
        })
      } catch (reason) {
        if (!disposed) {
          setState({
            kbDir,
            tasks: [],
            enrichments: [],
            graphExtractions: [],
            catalog: null,
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
    : { kbDir, tasks: [], enrichments: [], graphExtractions: [], catalog: null, error: null }
}
