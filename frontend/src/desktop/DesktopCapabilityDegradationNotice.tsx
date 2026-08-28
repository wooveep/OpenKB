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
  const offerModelSettings = uniqueCodes.some(needsModelSettings)
  const offerRetry = uniqueCodes.some(needsExplicitRetry)
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
        {uniqueCodes.map((code) => {
          const key = degradationKey(code)
          return (
            <li className="rounded-md border border-amber-500/25 bg-background/60 px-2.5 py-2" key={code}>
              <p className="font-medium">{t(`desktop.knowledgeBases.capabilityDegradations.${key}.title`)}</p>
              <p className="mt-0.5 leading-5">{t(`desktop.knowledgeBases.capabilityDegradations.${key}.impact`)}</p>
              <p className="mt-0.5 font-medium">{t(`desktop.knowledgeBases.capabilityDegradations.${key}.recovery`)}</p>
            </li>
          )
        })}
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

function degradationKey(code: string): DegradationKey {
  if (code === "retrieval_plan_unverified") return "retrievalPlan"
  if (code === "retrieval_plan_unavailable") return "retrievalPlanUnavailable"
  if (code === "retrieval_plan_cancelled") return "retrievalPlanCancelled"
  if (["retrieval_plan_fallback", "retrieval_plan_suspended"].includes(code)) return "retrievalPlanSuspended"
  if (code === "page_tree_selection_unverified") return "pageTreeSelection"
  if (code === "page_tree_selection_unavailable") return "pageTreeSelectionUnavailable"
  if (code === "page_tree_selection_cancelled") return "pageTreeSelectionCancelled"
  if (["page_tree_selection_failed", "page_tree_selection_invalid", "page_tree_selection_suspended"].includes(code)) return "pageTreeSelectionSuspended"
  if (code === "answer_model_unverified") return "answerModel"
  if (code === "answer_model_unavailable") return "answerModelUnavailable"
  if (code === "answer_model_fallback") return "answerModelFallback"
  if (code.startsWith("knowledge_graph_")) return "knowledgeGraph"
  return "generic"
}

function needsModelSettings(code: string): boolean {
  return [
    "retrieval_plan_unverified",
    "retrieval_plan_unavailable",
    "page_tree_selection_unverified",
    "page_tree_selection_unavailable",
    "answer_model_unverified",
    "answer_model_unavailable",
  ].includes(code)
}

function needsExplicitRetry(code: string): boolean {
  return [
    "retrieval_plan_cancelled",
    "retrieval_plan_fallback",
    "retrieval_plan_suspended",
    "page_tree_selection_cancelled",
    "page_tree_selection_failed",
    "page_tree_selection_invalid",
    "page_tree_selection_suspended",
    "answer_model_fallback",
  ].includes(code)
}
