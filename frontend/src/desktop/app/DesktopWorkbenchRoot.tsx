import { MotionConfig } from "motion/react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Toaster } from "@/components/ui/sonner"
import { DesktopBridgeProvider } from "@/desktop/bridge/context"
import DesktopKnowledgeBaseWorkspace from "@/desktop/app/DesktopKnowledgeBaseWorkspace"
import DesktopLocalSettingsPanel from "@/desktop/features/settings/DesktopLocalSettingsPanel"
import DesktopSensitiveTraceBanner from "@/desktop/features/diagnostics/DesktopSensitiveTraceBanner"
import DesktopStartup from "@/desktop/app/DesktopStartup"
import type { EngineViewPhase } from "@/desktop/app/DesktopStartup"
import type { DesktopBridge } from "@/desktop/bridge/contracts"

/** Desktop-only root for the supported local workbench. */
export default function DesktopWorkbenchRoot({ bridge }: { bridge?: DesktopBridge }) {
  const { t } = useTranslation("common")
  const [engineReady, setEngineReady] = useState(false)
  const [enginePhase, setEnginePhase] = useState<EngineViewPhase>("starting")
  const engineFailed = enginePhase === "error" || enginePhase === "unavailable"
  return (
    <DesktopBridgeProvider bridge={bridge}>
      <MotionConfig reducedMotion="user">
        <DesktopSensitiveTraceBanner />
        <DesktopKnowledgeBaseWorkspace engineReady={engineReady} />
        {!engineReady ? (
          <aside className={engineFailed ? "fixed inset-0 z-50 overflow-y-auto bg-background p-6" : "fixed bottom-4 right-4 z-50 w-[min(32rem,calc(100vw-2rem))]"} aria-label={t("desktop.workbench.title")}>
            <div className={engineFailed ? "mx-auto flex min-h-full max-w-3xl flex-col justify-center gap-5" : ""}>
              <DesktopStartup onReadyChange={setEngineReady} onPhaseChange={setEnginePhase} />
              {engineFailed ? <DesktopLocalSettingsPanel /> : null}
            </div>
          </aside>
        ) : null}
        <Toaster />
      </MotionConfig>
    </DesktopBridgeProvider>
  )
}
