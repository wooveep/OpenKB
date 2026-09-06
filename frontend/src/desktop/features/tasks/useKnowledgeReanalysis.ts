import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import type {
  DesktopBridge,
  DesktopKnowledgeReanalysisOverview,
} from "@/desktop/bridge/contracts"
import { nextDesktopRequestId } from "@/desktop/shared/request-id"
import { startPolling } from "@/desktop/shared/polling"

const EMPTY_OVERVIEW: DesktopKnowledgeReanalysisOverview = { documents: [], runs: [] }

interface ScopedOverview {
  kbDir: string | null
  value: DesktopKnowledgeReanalysisOverview
}

interface ScopedValue<T> {
  kbDir: string | null
  value: T
}

/** Keeps explicit Knowledge Reanalysis status synchronized with the active knowledge base. */
export function useKnowledgeReanalysis({
  bridge,
  kbDir,
  engineReady,
}: {
  bridge: DesktopBridge
  kbDir: string | null
  engineReady: boolean
}) {
  const [overviewState, setOverviewState] = useState<ScopedOverview>({
    kbDir: null,
    value: EMPTY_OVERVIEW,
  })
  const [workingState, setWorkingState] = useState<ScopedValue<string | null>>({
    kbDir: null,
    value: null,
  })
  const [errorState, setErrorState] = useState<ScopedValue<string | null>>({
    kbDir: null,
    value: null,
  })
  const refreshSequence = useRef(0)
  const activeContext = useRef({ kbDir, engineReady })

  useLayoutEffect(() => {
    activeContext.current = { kbDir, engineReady }
    refreshSequence.current += 1
  }, [engineReady, kbDir])

  const overview = overviewState.kbDir === kbDir ? overviewState.value : EMPTY_OVERVIEW
  const workingId = workingState.kbDir === kbDir ? workingState.value : null
  const error = errorState.kbDir === kbDir ? errorState.value : null

  const refresh = useCallback(async () => {
    const requestedKbDir = kbDir
    const requestedEngineReady = engineReady
    if (
      activeContext.current.kbDir !== requestedKbDir
      || activeContext.current.engineReady !== requestedEngineReady
    ) return
    const sequence = refreshSequence.current + 1
    refreshSequence.current = sequence
    if (!requestedKbDir || !requestedEngineReady) {
      setOverviewState({ kbDir: requestedKbDir, value: EMPTY_OVERVIEW })
      setErrorState({ kbDir: requestedKbDir, value: null })
      return
    }
    try {
      const result = await bridge.knowledgeReanalysis()
      if (
        sequence === refreshSequence.current
        && activeContext.current.kbDir === requestedKbDir
        && activeContext.current.engineReady === requestedEngineReady
      ) {
        setOverviewState({ kbDir: requestedKbDir, value: result })
        setErrorState({ kbDir: requestedKbDir, value: null })
      }
    } catch (reason) {
      if (
        sequence === refreshSequence.current
        && activeContext.current.kbDir === requestedKbDir
        && activeContext.current.engineReady === requestedEngineReady
      ) {
        setOverviewState({ kbDir: requestedKbDir, value: EMPTY_OVERVIEW })
        setErrorState({
          kbDir: requestedKbDir,
          value: reason instanceof Error ? reason.message : String(reason),
        })
      }
    }
  }, [bridge, engineReady, kbDir])

  useEffect(() => {
    void Promise.resolve().then(refresh)
  }, [refresh])

  const activeJobCount = useMemo(() => overview.runs.reduce(
    (count, run) => count + run.jobs.filter((job) => ["pending", "running"].includes(job.status)).length,
    0,
  ), [overview.runs])

  useEffect(() => {
    if (!activeJobCount) return
    return startPolling(refresh, 1_000, false)
  }, [activeJobCount, refresh])

  useEffect(() => {
    let disposed = false
    let unsubscribe: (() => void) | undefined
    if (!kbDir || !engineReady) return
    void bridge.subscribe((event) => {
      if (event.kind === "knowledge_reanalysis.updated") void refresh()
    }).then((remove) => {
      if (disposed) remove()
      else unsubscribe = remove
    }).catch(() => undefined)
    return () => {
      disposed = true
      unsubscribe?.()
    }
  }, [bridge, engineReady, kbDir, refresh])

  const start = useCallback(async (documentIds: string[]) => {
    const requestedKbDir = kbDir
    if (!requestedKbDir || !engineReady || !documentIds.length || workingId !== null) return
    setWorkingState({
      kbDir: requestedKbDir,
      value: documentIds.length === 1 ? documentIds[0] : "bulk",
    })
    setErrorState({ kbDir: requestedKbDir, value: null })
    try {
      await bridge.startKnowledgeReanalysis(
        documentIds,
        nextDesktopRequestId("knowledge-reanalysis"),
      )
      await refresh()
    } catch (reason) {
      if (activeContext.current.kbDir === requestedKbDir) {
        setErrorState({
          kbDir: requestedKbDir,
          value: reason instanceof Error ? reason.message : String(reason),
        })
      }
    } finally {
      setWorkingState((current) => current.kbDir === requestedKbDir
        ? { kbDir: requestedKbDir, value: null }
        : current)
    }
  }, [bridge, engineReady, kbDir, refresh, workingId])

  const retry = useCallback(async (jobId: string) => {
    const requestedKbDir = kbDir
    if (!requestedKbDir || !engineReady || workingId !== null) return
    setWorkingState({ kbDir: requestedKbDir, value: jobId })
    setErrorState({ kbDir: requestedKbDir, value: null })
    try {
      await bridge.retryKnowledgeReanalysis(
        jobId,
        nextDesktopRequestId("knowledge-reanalysis"),
      )
      await refresh()
    } catch (reason) {
      if (activeContext.current.kbDir === requestedKbDir) {
        setErrorState({
          kbDir: requestedKbDir,
          value: reason instanceof Error ? reason.message : String(reason),
        })
      }
    } finally {
      setWorkingState((current) => current.kbDir === requestedKbDir
        ? { kbDir: requestedKbDir, value: null }
        : current)
    }
  }, [bridge, engineReady, kbDir, refresh, workingId])

  return { overview, activeJobCount, workingId, error, refresh, start, retry }
}

export type KnowledgeReanalysisController = ReturnType<typeof useKnowledgeReanalysis>
