import { useTranslation } from "react-i18next"
import type { DesktopPageTreeEnrichmentTask } from "./contracts"

export function DesktopPageTreeEnrichmentTasks({
  tasks,
}: {
  tasks: DesktopPageTreeEnrichmentTask[]
}) {
  const { t } = useTranslation("common")
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
          {task.status === "running" && task.timeoutSeconds !== null ? (
            <p className="mt-2 text-xs text-muted-foreground">
              {t("desktop.tasks.pageTreeEnrichment.budget", {
                attempt: task.modelAttempt,
                timeout: Math.round(task.timeoutSeconds),
                remaining: Math.max(0, Math.round(task.remainingSeconds ?? 0)),
              })}
            </p>
          ) : null}
          {task.errorReason || task.errorCode ? (
            <p className="mt-2 text-xs text-destructive">
              {task.errorReason ?? task.errorCode}
            </p>
          ) : null}
        </article>
      ))}
    </section>
  )
}
