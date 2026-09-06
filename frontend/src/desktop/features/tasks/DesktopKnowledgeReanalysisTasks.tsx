import { Loader2, RefreshCw } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import type { KnowledgeReanalysisController } from "@/desktop/features/tasks/useKnowledgeReanalysis"

/** Bulk and per-document Knowledge Reanalysis progress inside Task Center. */
export function DesktopKnowledgeReanalysisTasks({
  controller,
}: {
  controller: KnowledgeReanalysisController
}) {
  const { t } = useTranslation("common")
  const { overview, workingId, error, start, retry } = controller
  const activeDocumentIds = new Set(overview.runs.flatMap((run) => run.jobs
    .filter((job) => ["pending", "running"].includes(job.status))
    .map((job) => job.documentId)))
  const eligible = overview.documents.filter(
    (document) => !activeDocumentIds.has(document.documentId),
  )

  return (
    <section className="mb-4 rounded-xl border border-border/70 bg-muted/20 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">{t("desktop.tasks.reanalysis.title")}</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            {t("desktop.tasks.reanalysis.description", { count: eligible.length })}
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={!eligible.length || workingId !== null}
          onClick={() => void start(eligible.map((document) => document.documentId))}
        >
          {workingId === "bulk" ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
          {t("desktop.tasks.reanalysis.startBulk")}
        </Button>
      </div>
      {error ? <p className="mt-3 text-sm text-destructive" role="alert">{error}</p> : null}
      {overview.runs.length ? (
        <div className="mt-4 space-y-3">
          {overview.runs.slice(0, 5).map((run) => (
            <article key={run.runId} className="rounded-lg border border-border/70 bg-background p-3">
              <div className="flex items-center justify-between gap-3 text-xs">
                <span className="font-medium">
                  {t(`desktop.tasks.reanalysis.modes.${run.mode}`)} · {t(`desktop.tasks.reanalysis.statuses.${run.status}`)}
                </span>
                <span className="text-muted-foreground">
                  {t("desktop.tasks.reanalysis.runSummary", {
                    total: run.total,
                    completed: run.completed,
                    failed: run.failed,
                  })}
                </span>
              </div>
              <div className="mt-3 space-y-2">
                {run.jobs.map((job) => (
                  <div key={job.jobId} className="rounded-md bg-muted/30 p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium">{job.documentName}</p>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {t(`desktop.tasks.reanalysis.phases.${job.phase}`)} · {job.provider}/{job.model}
                        </p>
                      </div>
                      <span className="text-xs font-medium">{job.progress}%</span>
                    </div>
                    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
                      <div className="h-full rounded-full bg-primary" style={{ width: `${job.progress}%` }} />
                    </div>
                    {job.batchTotal ? (
                      <p className="mt-2 text-xs text-muted-foreground">
                        {t("desktop.tasks.reanalysis.batchProgress", {
                          completed: job.batchCompleted,
                          total: job.batchTotal,
                          current: job.currentBatch ?? "—",
                        })}
                      </p>
                    ) : null}
                    {job.attemptCount !== null ? (
                      <p className="mt-1 text-xs text-muted-foreground">
                        {t("desktop.tasks.reanalysis.attempt", {
                          attempt: job.attemptCount,
                        })}
                      </p>
                    ) : null}
                    {job.reason ? <p className="mt-2 text-xs text-destructive">{job.reason}</p> : null}
                    {job.status === "failed" ? (
                      <Button
                        className="mt-2"
                        size="sm"
                        variant="outline"
                        disabled={workingId !== null}
                        onClick={() => void retry(job.jobId)}
                      >
                        {workingId === job.jobId ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                        {t("desktop.tasks.reanalysis.retry")}
                      </Button>
                    ) : null}
                  </div>
                ))}
              </div>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  )
}
