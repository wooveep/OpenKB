import { MotionConfig } from "motion/react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Toaster } from "@/components/ui/sonner"
import { DesktopBridgeProvider } from "./bridge-context"
import DesktopKnowledgeBaseWorkspace from "./DesktopKnowledgeBaseWorkspace"
import DesktopStartup from "./DesktopStartup"
import type { DesktopBridge } from "./contracts"

/** Desktop-only root. Future workbench views extend this instead of the REST UI. */
export default function DesktopWorkbenchRoot({ bridge }: { bridge?: DesktopBridge }) {
  const { t } = useTranslation("common")
  const [engineReady, setEngineReady] = useState(false)
  return (
    <DesktopBridgeProvider bridge={bridge}>
      <MotionConfig reducedMotion="user">
        {engineReady ? (
          <DesktopKnowledgeBaseWorkspace />
        ) : (
          <main className="ambient-ground flex min-h-screen items-center justify-center p-6">
            <div className="w-full max-w-2xl">
              <div className="mb-7">
                <p className="font-mono2 text-xs font-medium tracking-[0.18em] text-accent-brand">OPENKB</p>
                <h2 className="mt-2 text-3xl font-semibold tracking-tight text-foreground">
                  {t("desktop.workbench.title")}
                </h2>
                <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">
                  {t("desktop.workbench.detail")}
                </p>
              </div>
              <DesktopStartup onReadyChange={setEngineReady} />
            </div>
          </main>
        )}
        <Toaster />
      </MotionConfig>
    </DesktopBridgeProvider>
  )
}
