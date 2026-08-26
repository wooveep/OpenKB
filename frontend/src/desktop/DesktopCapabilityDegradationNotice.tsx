import { AlertTriangle } from "lucide-react"
import { useTranslation } from "react-i18next"

/** Make optional retrieval fallbacks visible without treating a safe answer as failed. */
export function DesktopCapabilityDegradationNotice({ codes }: { codes: string[] }) {
  const { t } = useTranslation("common")
  if (!codes.length) return null
  return (
    <section
      className="mt-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
      data-testid="capability-degradation"
    >
      <p className="flex items-center gap-1.5 font-medium">
        <AlertTriangle className="size-3.5" />
        {t("desktop.knowledgeBases.capabilityDegradationTitle")}
      </p>
      <p className="mt-1">{t("desktop.knowledgeBases.capabilityDegradationDescription")}</p>
      <ul className="mt-1 list-disc pl-4 font-mono">
        {codes.map((code) => <li key={code}>{code}</li>)}
      </ul>
    </section>
  )
}
