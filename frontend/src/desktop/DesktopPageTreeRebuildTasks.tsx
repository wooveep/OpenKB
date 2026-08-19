import { useTranslation } from "react-i18next"
import type { DesktopPageTreeRebuildTask } from "./contracts"

export function DesktopPageTreeRebuildTasks({
  tasks,
  error,
}: {
  tasks: DesktopPageTreeRebuildTask[]
  error: string | null
}) {
  const { t } = useTranslation("common")
  if (!tasks.length && !error) return null
  return (
    <section className="mb-4 space-y-2" aria-label={t("desktop.tasks.pageTreeTitle")}>
      <h3 className="text-sm font-semibold">{t("desktop.tasks.pageTreeTitle")}</h3>
      {error ? <p className="text-xs text-destructive">{error}</p> : null}
      {tasks.map((task) => (
        <article key={task.documentId} className="rounded-xl border border-border/70 bg-muted/20 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{task.documentName}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {t(`desktop.tasks.pageTreeStatuses.${task.status}`)}
                {" · "}
                {t("desktop.tasks.pageTreeProvider", {
                  provider: task.providerKind,
                  version: task.providerVersion,
                })}
              </p>
            </div>
            <span className="text-xs text-muted-foreground">
              {t("desktop.tasks.pageTreeAttempts", { count: task.attemptCount })}
            </span>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            {t(`desktop.tasks.pageTreeReasons.${task.reason}`, { defaultValue: task.reason })}
          </p>
          {task.errorCode ? <p className="mt-2 text-xs text-destructive">{task.errorCode}</p> : null}
        </article>
      ))}
    </section>
  )
}
