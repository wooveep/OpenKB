import { useTranslation } from "react-i18next"
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/drawer"
import { Button } from "@/components/ui/button"
import type { DesktopImportBatchSummary } from "./DesktopDocumentImportPanel"
import type { DesktopImportTask } from "./contracts"

type ImportTaskAction = "pause" | "resume" | "cancel"

/** Global bottom drawer for import jobs and their current stage runs. */
export function DesktopTaskDrawer({
  open,
  batchSummary,
  tasks,
  controllingJobId,
  onOpenChange,
  onControl,
}: {
  open: boolean
  batchSummary: DesktopImportBatchSummary | null
  tasks: DesktopImportTask[]
  controllingJobId: string | null
  onOpenChange: (open: boolean) => void
  onControl: (jobId: string, action: ImportTaskAction) => void
}) {
  const { t } = useTranslation("common")
  const batchTotal = batchSummary?.total ?? 0
  const batchActive = batchSummary ? Math.max(0, batchSummary.total - batchSummary.completed - batchSummary.failures.length) : 0
  return (
    <Drawer open={open} onOpenChange={onOpenChange}>
      <DrawerContent className="max-h-[72vh]">
        <div className="mx-auto w-full max-w-5xl overflow-y-auto px-4 pb-6">
          <DrawerHeader className="px-0">
            <DrawerTitle>{t("desktop.tasks.title")}</DrawerTitle>
            <DrawerDescription>{t("desktop.tasks.description")}</DrawerDescription>
          </DrawerHeader>
          {tasks.length || batchSummary ? (
            <div className="space-y-2">
              {batchSummary ? <section className="mb-4 rounded-xl border border-border/70 bg-muted/20 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold">{t("desktop.tasks.batch")}</h3>
                    <p className="mt-1 text-xs text-muted-foreground">{t("desktop.tasks.batchSummary", { total: batchTotal, completed: batchSummary.completed, active: batchActive, failed: batchSummary.failures.length })}</p>
                  </div>
                  <span className="text-sm font-medium">{batchTotal ? Math.round(((batchSummary.completed + batchSummary.failures.length) / batchTotal) * 100) : 0}%</span>
                </div>
              </section> : null}
              {tasks.map((task) => {
                const activeStage = task.stages.find((stage) => stage.status === "running")
                  ?? task.stages.find((stage) => ["failed", "paused"].includes(stage.status))
                  ?? task.stages.at(-1)
                const controllable = ["running", "paused", "recoverable"].includes(task.job.status)
                return (
                  <article key={task.job.jobId} className="rounded-xl border border-border/70 bg-background p-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div className="min-w-0">
                        <h3 className="truncate text-sm font-medium">{task.job.sourceName}</h3>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {activeStage ? t(`desktop.knowledgeBases.importStages.${activeStage.stage}`) : "—"}
                          {" · "}{t(`desktop.knowledgeBases.importStatuses.${task.job.status}`)}
                        </p>
                      </div>
                      <span className="text-sm font-medium">{task.job.progress}%</span>
                    </div>
                    <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${task.job.progress}%` }} /></div>
                    <ol className="mt-3 flex flex-wrap gap-1.5" aria-label={t("desktop.tasks.stages")}>
                      {task.stages.map((stage) => (
                        <li key={stage.stageRunId} className="rounded-full border border-border/70 bg-muted/20 px-2 py-1 text-[11px] text-muted-foreground">
                          {t(`desktop.knowledgeBases.importStages.${stage.stage}`)} · {t(`desktop.knowledgeBases.importStatuses.${stage.status}`)}
                        </li>
                      ))}
                    </ol>
                    {controllable ? (
                      <div className="mt-3 flex gap-2">
                        {task.job.status === "running" ? <Button size="sm" variant="outline" disabled={controllingJobId === task.job.jobId} onClick={() => onControl(task.job.jobId, "pause")}>{t("desktop.knowledgeBases.pauseImport")}</Button> : null}
                        {["paused", "recoverable"].includes(task.job.status) ? <Button size="sm" disabled={controllingJobId === task.job.jobId} onClick={() => onControl(task.job.jobId, "resume")}>{t("desktop.knowledgeBases.resumeImport")}</Button> : null}
                        <Button size="sm" variant="ghost" disabled={controllingJobId === task.job.jobId} onClick={() => onControl(task.job.jobId, "cancel")}>{t("desktop.knowledgeBases.cancelImport")}</Button>
                      </div>
                    ) : null}
                  </article>
                )
              })}
            </div>
          ) : <p className="py-8 text-center text-sm text-muted-foreground">{t("desktop.tasks.empty")}</p>}
        </div>
      </DrawerContent>
    </Drawer>
  )
}
