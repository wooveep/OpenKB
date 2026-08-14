import { createContext, useContext, useMemo, type ReactNode } from "react"
import { createDesktopBridge } from "./bridge"
import type { DesktopBridge } from "./contracts"

const DesktopBridgeContext = createContext<DesktopBridge | null>(null)

/** Lets components consume a replaceable Bridge instead of Tauri directly. */
export function DesktopBridgeProvider({
  bridge,
  children,
}: {
  bridge?: DesktopBridge
  children: ReactNode
}) {
  const value = useMemo(() => bridge ?? createDesktopBridge(), [bridge])
  return <DesktopBridgeContext.Provider value={value}>{children}</DesktopBridgeContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useDesktopBridge(): DesktopBridge {
  const bridge = useContext(DesktopBridgeContext)
  if (!bridge) throw new Error("DesktopBridgeProvider is required for Desktop Workbench components.")
  return bridge
}
