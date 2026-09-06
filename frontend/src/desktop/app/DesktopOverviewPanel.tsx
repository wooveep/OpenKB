import { AlertTriangle, FileCheck2, Loader2, MessageSquare, Scale, Upload } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { useDesktopBridge } from "@/desktop/bridge/context"
import type { DesktopImportTask } from "@/desktop/bridge/contracts"
import { currentDocumentTasks, taskIsFailed } from "@/desktop/features/documents/desktop-document-tasks"

/** Actionable summary for the active knowledge base, without repeating technical metadata. */
export function DesktopOverviewPanel({
  tasks,
  onImport,
  onStartConversation,
  onOpenReview,
  onOpenFailures,
}: {
  tasks: DesktopImportTask[]
  onImport: () => void
  onStartConversation: () => void
  onOpenReview: () => void
  onOpenFailures: () => void
}) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [reviewCount, setReviewCount] = useState(0)

  useEffect(() => {
    let disposed = false
    void Promise.all([
      bridge.knowledgeReconciliationConflicts(),
      bridge.documentVersionCandidates(),
      bridge.missingSourceCandidates(),
    ]).then(([conflicts, versions, missingSources]) => {
      if (!disposed) {
        setReviewCount(
          conflicts.conflicts.length
          + versions.candidates.filter((item) => item.status === "pending").length
          + missingSources.candidates.length,
        )
      }
    }).catch(() => undefined)
    return () => { disposed = true }
  }, [bridge, tasks])

  const documents = currentDocumentTasks(tasks)
  const available = documents.filter((task) => task.document?.availability === "available").length
  const processing = documents.filter((task) => ["running", "paused", "recoverable"].includes(task.job.status)).length
  const failed = documents.filter(taskIsFailed).length
  const metrics = [
    { label: t("desktop.overview.available"), value: available, icon: FileCheck2, action: onImport },
    { label: t("desktop.overview.processing"), value: processing, icon: Loader2, action: onImport },
    { label: t("desktop.overview.failed"), value: failed, icon: AlertTriangle, action: onOpenFailures },
    { label: t("desktop.overview.review"), value: reviewCount, icon: Scale, action: onOpenReview },
  ]

  return (
    <div className="mx-auto max-w-5xl space-y-8 py-2" data-testid="desktop-overview">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("desktop.overview.title")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("desktop.overview.description")}</p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {metrics.map(({ label, value, icon: Icon, action }) => (
          <button
            key={label}
            type="button"
            onClick={action}
            className="flex items-center gap-3 rounded-xl border border-border/70 bg-background p-4 text-left shadow-sm outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="grid size-9 place-items-center rounded-lg bg-primary/10 text-primary"><Icon className="size-4" /></span>
            <span><strong className="block text-xl">{value}</strong><span className="text-xs text-muted-foreground">{label}</span></span>
          </button>
        ))}
      </div>
      <div className="flex flex-wrap gap-3">
        <Button onClick={onImport}><Upload className="size-4" />{t("desktop.overview.importDocuments")}</Button>
        <Button variant="outline" onClick={onStartConversation}><MessageSquare className="size-4" />{t("desktop.overview.startConversation")}</Button>
      </div>
      <section>
        <h2 className="text-sm font-semibold">{t("desktop.overview.recentActivity")}</h2>
        {tasks.length ? (
          <ul className="mt-3 divide-y divide-border/70 rounded-xl border border-border/70 bg-background">
            {tasks.slice(0, 6).map((task) => (
              <li key={task.job.jobId} className="flex items-center justify-between gap-4 px-4 py-3 text-sm">
                <span className="min-w-0 truncate">{task.job.sourceName}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {t(`desktop.knowledgeBases.importStatuses.${task.job.status}`)} · {task.job.progress}%
                </span>
              </li>
            ))}
          </ul>
        ) : <p className="mt-3 text-sm text-muted-foreground">{t("desktop.overview.noActivity")}</p>}
      </section>
    </div>
  )
}
