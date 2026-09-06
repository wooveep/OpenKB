import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import type { DesktopBridge, DesktopPageTreeEnrichmentTask } from "@/desktop/bridge/contracts"
import { DesktopModelActivityDetails } from "@/desktop/features/tasks/DesktopModelActivityDetails"

type EnrichmentAction = "cancel" | "resume" | "retry"

export function DesktopPageTreeEnrichmentTasks({
  tasks,
  bridge,
}: {
  tasks: DesktopPageTreeEnrichmentTask[]
  bridge: DesktopBridge
}) {
  const { t } = useTranslation("common")
  const [controllingDocumentId, setControllingDocumentId] = useState<string | null>(null)
  const [controlError, setControlError] = useState<string | null>(null)
  const [cancellationWarning, setCancellationWarning] = useState(false)

  const control = async (task: DesktopPageTreeEnrichmentTask, action: EnrichmentAction) => {
    setControllingDocumentId(task.documentId)
    setControlError(null)
    try {
      const result = action === "cancel"
        ? await bridge.cancelPageTreeEnrichment(task.documentId)
        : await bridge.retryPageTreeEnrichment(task.documentId)
      if (!result.accepted) {
        setControlError(t("desktop.tasks.pageTreeEnrichment.actionRejected"))
        return
      }
      setCancellationWarning(action === "cancel")
    } catch (reason) {
      setControlError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setControllingDocumentId(null)
    }
  }

  if (!tasks.length) return null
  return (
    <section className="mb-4 space-y-2" aria-label={t("desktop.tasks.pageTreeEnrichment.title")}>
      <div>
        <h3 className="text-sm font-semibold">{t("desktop.tasks.pageTreeEnrichment.title")}</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          {t("desktop.tasks.pageTreeEnrichment.description")}
        </p>
      </div>
      {tasks.map((task) => (
        <article key={task.documentId} className="rounded-xl border border-border/70 bg-muted/20 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{task.documentName}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {t(`desktop.tasks.pageTreeEnrichment.statuses.${task.status}`)}
                {" · "}{task.provider} / {task.model}
              </p>
            </div>
            <span className="text-xs text-muted-foreground">
              {t("desktop.tasks.pageTreeEnrichment.attempts", { count: task.attemptCount })}
            </span>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            {t(`desktop.tasks.pageTreeEnrichment.reasons.${task.reason}`, {
              defaultValue: task.reason,
            })}
          </p>
          {task.status === "running" && task.modelAttempt > 0 ? (
            <p className="mt-2 text-xs text-muted-foreground">
              {t("desktop.tasks.pageTreeEnrichment.budget", {
                attempt: task.modelAttempt,
              })}
            </p>
          ) : null}
          {task.modelActivity ? (
            <div className="mt-3 text-xs">
              <DesktopModelActivityDetails activity={task.modelActivity} />
            </div>
          ) : task.callId ? (
            <p className="mt-2 break-all font-mono text-xs text-muted-foreground">
              {t("desktop.tasks.pageTreeEnrichment.callIdentity", {
                callId: task.callId,
                attemptId: `${task.callId}:${task.modelAttempt}`,
              })}
            </p>
          ) : null}
          {task.errorReason || task.errorCode ? (
            <p className="mt-2 text-xs text-destructive">
              {task.errorReason ?? task.errorCode}
            </p>
          ) : null}
          {task.status === "running" ? (
            <Button
              className="mt-3"
              size="sm"
              variant="outline"
              disabled={controllingDocumentId === task.documentId}
              onClick={() => void control(task, "cancel")}
            >
              {t("desktop.tasks.pageTreeEnrichment.cancel")}
            </Button>
          ) : task.status === "pending"
            && task.errorCode === "page_tree_enrichment_interrupted" ? (
              <Button
                className="mt-3"
                size="sm"
                disabled={controllingDocumentId === task.documentId}
                onClick={() => void control(task, "resume")}
              >
                {t("desktop.tasks.pageTreeEnrichment.resume")}
              </Button>
            ) : task.status === "failed" ? (
              <Button
                className="mt-3"
                size="sm"
                disabled={controllingDocumentId === task.documentId}
                onClick={() => void control(task, "retry")}
              >
                {t("desktop.tasks.pageTreeEnrichment.retry")}
              </Button>
            ) : null}
        </article>
      ))}
      {cancellationWarning ? (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          {t("desktop.tasks.pageTreeEnrichment.cancelWarning")}
        </p>
      ) : null}
      {controlError ? <p className="text-xs text-destructive">{controlError}</p> : null}
    </section>
  )
}
