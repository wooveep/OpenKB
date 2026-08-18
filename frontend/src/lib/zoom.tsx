/* eslint-disable react-refresh/only-export-components -- provider and hook are one small preference module */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react"

const KEY = "openkb_zoom"
const MIN_ZOOM = 80
const MAX_ZOOM = 200

const ZoomContext = createContext<{ zoom: number; setZoom: (zoom: number) => void } | null>(null)

function normalizedZoom(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(value / 10) * 10))
}

export function ZoomProvider({ children }: { children: ReactNode }) {
  const [zoom, setZoomState] = useState(() => normalizedZoom(Number(localStorage.getItem(KEY)) || 100))
  const setZoom = useCallback((value: number) => {
    const next = normalizedZoom(value)
    localStorage.setItem(KEY, String(next))
    setZoomState(next)
  }, [])

  useEffect(() => {
    document.documentElement.style.fontSize = `${zoom}%`
    return () => { document.documentElement.style.removeProperty("font-size") }
  }, [zoom])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return
      if (event.key === "0") {
        event.preventDefault()
        setZoom(100)
      } else if (event.key === "+" || event.key === "=") {
        event.preventDefault()
        setZoom(zoom + 10)
      } else if (event.key === "-") {
        event.preventDefault()
        setZoom(zoom - 10)
      }
    }
    window.addEventListener("keydown", handleKeyDown)
    return () => window.removeEventListener("keydown", handleKeyDown)
  }, [setZoom, zoom])

  return <ZoomContext.Provider value={{ zoom, setZoom }}>{children}</ZoomContext.Provider>
}

export function useZoom() {
  const context = useContext(ZoomContext)
  if (!context) throw new Error("useZoom must be used within ZoomProvider")
  return context
}
