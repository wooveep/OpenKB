import { useTranslation } from "react-i18next"
import type { DesktopKnowledgeAnalysisProgress as Progress } from "@/desktop/bridge/contracts"

/** Shared long-document Knowledge Analysis Batch projection. */
export function DesktopKnowledgeAnalysisProgress({ progress }: { progress: Progress }) {
  const { t } = useTranslation("common")
  return (
    <div className="mt-3 rounded-lg border border-border/70 bg-muted/20 p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">{t("desktop.tasks.knowledgeAnalysis")}</span>
        <span className="text-muted-foreground">
          {t(`desktop.tasks.knowledgeAnalysisPhases.${progress.phase}`)}
        </span>
      </div>
      <p className="mt-1 text-muted-foreground">
        {t("desktop.tasks.knowledgeAnalysisSummary", {
          total: progress.total,
          completed: progress.completed,
          active: progress.active,
          failed: progress.failed,
        })}
      </p>
      {progress.currentBatch !== null && progress.phase === "batches" ? (
        <p className="mt-1 text-muted-foreground">
          {t("desktop.tasks.knowledgeAnalysisCurrent", {
            current: progress.currentBatch,
            total: progress.total,
          })}
        </p>
      ) : null}
    </div>
  )
}
