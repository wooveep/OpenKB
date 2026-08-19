import { useTranslation } from "react-i18next"
import type { DesktopCatalogRebuildTask as CatalogTask } from "./contracts"

export function DesktopCatalogRebuildTask({ task }: { task: CatalogTask | null }) {
  const { t } = useTranslation("common")
  if (!task) return null
  return (
    <section className="mb-4 space-y-2" aria-label={t("desktop.tasks.catalog.title")}>
      <h3 className="text-sm font-semibold">{t("desktop.tasks.catalog.title")}</h3>
      <article className="rounded-xl border border-border/70 bg-muted/20 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium">
              {t(`desktop.tasks.catalog.statuses.${task.status}`)}
              {task.staleServing ? ` · ${t("desktop.tasks.catalog.staleServing")}` : ""}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t(`desktop.tasks.catalog.reasons.${task.reason}`, { defaultValue: task.reason })}
            </p>
          </div>
          <span className="text-xs text-muted-foreground">
            {t("desktop.tasks.catalog.counts", {
              nodes: task.nodeCount,
              links: task.linkCount,
              attempts: task.attemptCount,
            })}
          </span>
        </div>
        {task.errorCode ? (
          <p className="mt-2 text-xs text-destructive">
            {task.errorCode}{task.errorReason ? ` · ${task.errorReason}` : ""}
          </p>
        ) : null}
      </article>
    </section>
  )
}
