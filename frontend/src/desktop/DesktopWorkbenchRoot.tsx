import { MotionConfig } from "motion/react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Toaster } from "@/components/ui/sonner"
import { DesktopBridgeProvider } from "./bridge-context"
import DesktopKnowledgeBaseWorkspace from "./DesktopKnowledgeBaseWorkspace"
import DesktopStartup from "./DesktopStartup"
import type { DesktopBridge } from "./contracts"

/** Desktop-only root for the supported local workbench. */
export default function DesktopWorkbenchRoot({ bridge }: { bridge?: DesktopBridge }) {
  const { t } = useTranslation("common")
  const [engineReady, setEngineReady] = useState(false)
  return (
    <DesktopBridgeProvider bridge={bridge}>
      <MotionConfig reducedMotion="user">
        <DesktopKnowledgeBaseWorkspace />
        {!engineReady ? (
          <aside className="fixed bottom-4 right-4 z-50 w-[min(32rem,calc(100vw-2rem))]" aria-label={t("desktop.workbench.title")}>
            <DesktopStartup onReadyChange={setEngineReady} />
          </aside>
        ) : null}
        <Toaster />
      </MotionConfig>
    </DesktopBridgeProvider>
  )
}
