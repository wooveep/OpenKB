/** Schedule the next refresh only after the current one has settled. */
export function startPolling(
  refresh: () => Promise<unknown>,
  intervalMs: number,
  immediate = true,
): () => void {
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const run = async () => {
    try {
      await refresh()
    } catch {
      // Refresh owns its error state; a transient failure must not stop polling.
    } finally {
      if (!disposed) timer = setTimeout(run, intervalMs)
    }
  }
  if (immediate) void run()
  else timer = setTimeout(run, intervalMs)
  return () => {
    disposed = true
    if (timer !== undefined) clearTimeout(timer)
  }
}
