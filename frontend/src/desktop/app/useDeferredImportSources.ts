import { useCallback, useEffect, useRef } from "react"

/** Keeps new source selections until the current sequential import batch finishes. */
export function useDeferredImportSources(
  importing: boolean,
  selectImportSources: (sourcePaths: string[]) => void,
): (sourcePaths: string[]) => void {
  const pendingSourcePaths = useRef<string[]>([])

  useEffect(() => {
    if (importing || !pendingSourcePaths.current.length) return
    const sourcePaths = pendingSourcePaths.current
    pendingSourcePaths.current = []
    selectImportSources(sourcePaths)
  }, [importing, selectImportSources])

  return useCallback((sourcePaths: string[]) => {
    if (importing) {
      pendingSourcePaths.current.push(...sourcePaths)
      return
    }
    selectImportSources(sourcePaths)
  }, [importing, selectImportSources])
}
