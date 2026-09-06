export interface LatestRefresh {
  request(): void
  dispose(): void
}

/**
 * Runs at most one load at a time and commits only the newest requested result.
 * Requests arriving during a load collapse into one trailing refresh.
 */
export function createLatestRefresh<T>({
  load,
  commit,
  onError = () => undefined,
}: {
  load: () => Promise<T>
  commit: (value: T) => void
  onError?: (error: unknown) => void
}): LatestRefresh {
  let disposed = false
  let requested = false
  let running = false

  const drain = async () => {
    if (disposed || running) return
    running = true
    try {
      while (!disposed && requested) {
        requested = false
        try {
          const value = await load()
          if (!disposed && !requested) commit(value)
        } catch (error) {
          if (!disposed && !requested) onError(error)
        }
      }
    } finally {
      running = false
      if (!disposed && requested) void drain()
    }
  }

  return {
    request() {
      if (disposed) return
      requested = true
      void drain()
    },
    dispose() {
      disposed = true
      requested = false
    },
  }
}
