import { AlertTriangle, RotateCcw, Settings } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"

/** Make optional retrieval fallbacks visible without treating a safe answer as failed. */
export function DesktopCapabilityDegradationNotice({
  codes,
  onOpenModelSettings,
  onRetry,
}: {
  codes: string[]
  onOpenModelSettings?: () => void
  onRetry?: () => void
}) {
  const { t } = useTranslation("common")
  if (!codes.length) return null
  const uniqueCodes = [...new Set(codes)]
  const degradations = uniqueCodes.map((code) => ({ code, ...degradationMetadata(code) }))
  const offerModelSettings = degradations.some(({ action }) => action === "model_settings")
  const offerRetry = degradations.some(({ action }) => action === "retry")
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
      <ul className="mt-2 space-y-2">
        {degradations.map(({ code, key }) => (
            <li className="rounded-md border border-amber-500/25 bg-background/60 px-2.5 py-2" key={code}>
              <p className="font-medium">{t(`desktop.knowledgeBases.capabilityDegradations.${key}.title`)}</p>
              <p className="mt-0.5 leading-5">{t(`desktop.knowledgeBases.capabilityDegradations.${key}.impact`)}</p>
              <p className="mt-0.5 font-medium">{t(`desktop.knowledgeBases.capabilityDegradations.${key}.recovery`)}</p>
            </li>
        ))}
      </ul>
      {offerModelSettings && onOpenModelSettings || offerRetry && onRetry ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {offerModelSettings && onOpenModelSettings ? (
            <Button type="button" size="sm" variant="outline" onClick={onOpenModelSettings}>
              <Settings className="size-3.5" />
              {t("desktop.knowledgeBases.capabilityDegradationOpenSettings")}
            </Button>
          ) : null}
          {offerRetry && onRetry ? (
            <Button type="button" size="sm" variant="outline" onClick={onRetry}>
              <RotateCcw className="size-3.5" />
              {t("desktop.knowledgeBases.capabilityDegradationRetry")}
            </Button>
          ) : null}
        </div>
      ) : null}
      <details className="mt-2">
        <summary className="cursor-pointer font-medium">{t("desktop.knowledgeBases.capabilityDegradationTechnicalDetails")}</summary>
        <ul className="mt-1 list-disc pl-4 font-mono">
          {uniqueCodes.map((code) => <li key={code}>{code}</li>)}
        </ul>
      </details>
    </section>
  )
}

type DegradationKey =
  | "retrievalPlan"
  | "retrievalPlanUnavailable"
  | "retrievalPlanCancelled"
  | "retrievalPlanSuspended"
  | "pageTreeSelection"
  | "pageTreeSelectionUnavailable"
  | "pageTreeSelectionCancelled"
  | "pageTreeSelectionSuspended"
  | "answerModel"
  | "answerModelUnavailable"
  | "answerModelFallback"
  | "knowledgeGraph"
  | "generic"

type DegradationAction = "model_settings" | "retry" | null

type DegradationMetadata = {
  key: DegradationKey
  action: DegradationAction
}

const DEGRADATION_METADATA: Readonly<Record<string, DegradationMetadata>> = {
  retrieval_plan_unverified: { key: "retrievalPlan", action: "model_settings" },
  retrieval_plan_unavailable: { key: "retrievalPlanUnavailable", action: "model_settings" },
  retrieval_plan_cancelled: { key: "retrievalPlanCancelled", action: "retry" },
  retrieval_plan_fallback: { key: "retrievalPlanSuspended", action: "retry" },
  retrieval_plan_suspended: { key: "retrievalPlanSuspended", action: "retry" },
  page_tree_selection_unverified: { key: "pageTreeSelection", action: "model_settings" },
  page_tree_selection_unavailable: { key: "pageTreeSelectionUnavailable", action: "model_settings" },
  page_tree_selection_cancelled: { key: "pageTreeSelectionCancelled", action: "retry" },
  page_tree_selection_failed: { key: "pageTreeSelectionSuspended", action: "retry" },
  page_tree_selection_invalid: { key: "pageTreeSelectionSuspended", action: "retry" },
  page_tree_selection_suspended: { key: "pageTreeSelectionSuspended", action: "retry" },
  answer_model_unverified: { key: "answerModel", action: "model_settings" },
  answer_model_unavailable: { key: "answerModelUnavailable", action: "model_settings" },
  answer_model_fallback: { key: "answerModelFallback", action: "retry" },
}

function degradationMetadata(code: string): DegradationMetadata {
  const known = DEGRADATION_METADATA[code]
  if (known) return known
  if (code.startsWith("knowledge_graph_")) {
    return { key: "knowledgeGraph", action: null }
  }
  return { key: "generic", action: null }
}
