import { FolderOpen, OctagonX, ShieldAlert } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import {
  desktopDiagnosticStatus,
  revealDesktopSensitiveTraceDirectory,
  stopDesktopSensitiveTrace,
  type DesktopDiagnosticStatus,
} from "./desktop-diagnostics"

const STATUS_REFRESH_MS = 2_000

/** Persistent disclosure and containment controls for an active raw trace capture. */
export default function DesktopSensitiveTraceBanner() {
  const { t, i18n } = useTranslation("common")
  const [status, setStatus] = useState<DesktopDiagnosticStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const refresh = async () => {
      try {
        const next = await desktopDiagnosticStatus()
        if (!cancelled) setStatus(next)
      } catch {
        // Browser-only development has no Tauri command surface.
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), STATUS_REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  if (!status?.sensitiveTraceActive) return null

  const stop = async () => {
    setError(null)
    try {
      setStatus(await stopDesktopSensitiveTrace())
    } catch (caught) {
      setError(String(caught))
    }
  }
  const reveal = async () => {
    if (!window.confirm(t("desktop.sensitiveTrace.revealConfirmation"))) return
    setError(null)
    try {
      await revealDesktopSensitiveTraceDirectory()
    } catch (caught) {
      setError(String(caught))
    }
  }
  const expiry = status.sensitiveTraceExpiresAt
    ? new Intl.DateTimeFormat(i18n.language, { dateStyle: "short", timeStyle: "medium" }).format(
        new Date(status.sensitiveTraceExpiresAt),
      )
    : t("desktop.sensitiveTrace.unknownExpiry")

  return (
    <aside
      className="fixed inset-x-0 top-0 z-[70] border-b border-red-700 bg-red-950 px-3 py-2 text-red-50 shadow-xl"
      role="alert"
      data-testid="sensitive-trace-banner"
    >
      <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-x-3 gap-y-2">
        <ShieldAlert className="size-5 shrink-0 text-red-300" />
        <div className="min-w-0 flex-1 text-xs leading-5">
          <p className="font-semibold">{t("desktop.sensitiveTrace.title")}</p>
          <p className="text-red-100">
            {t("desktop.sensitiveTrace.detail", {
              captureId: status.sensitiveTraceCaptureId,
              expiry,
              size: formatBytes(status.sensitiveTraceSizeBytes),
              components: status.traceComponents.join(", "),
            })}
          </p>
          {error ? <p className="font-medium text-red-200">{error}</p> : null}
        </div>
        <Button type="button" size="sm" variant="secondary" onClick={() => void reveal()}>
          <FolderOpen className="size-3.5" />
          {t("desktop.sensitiveTrace.openDirectory")}
        </Button>
        <Button type="button" size="sm" variant="destructive" onClick={() => void stop()}>
          <OctagonX className="size-3.5" />
          {t("desktop.sensitiveTrace.stop")}
        </Button>
      </div>
    </aside>
  )
}

function formatBytes(bytes: number): string {
  if (bytes < 1_024) return `${bytes} B`
  if (bytes < 1_024 * 1_024) return `${(bytes / 1_024).toFixed(1)} KiB`
  return `${(bytes / (1_024 * 1_024)).toFixed(1)} MiB`
}
