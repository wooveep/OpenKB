import { useTranslation } from "react-i18next"
import type {
  DesktopImportProgressStep,
  DesktopModelActivity,
  DesktopModelUsageAggregate,
  DesktopModelUsageRecord,
} from "@/desktop/bridge/contracts"
import { DesktopModelActivityDetails } from "@/desktop/features/tasks/DesktopModelActivityDetails"

/** Durable import checkpoints plus content-free model activity; never inferred percentages. */
export function DesktopImportProgress({
  steps,
  activity,
  usage,
  records,
}: {
  steps: DesktopImportProgressStep[]
  activity: DesktopModelActivity | null
  usage: DesktopModelUsageAggregate | null
  records: DesktopModelUsageRecord[]
}) {
  const { t } = useTranslation("common")
  if (!steps.length && !activity && !usage && !records.length) return null
  return (
    <section className="mt-3 rounded-lg border border-border/70 bg-muted/20 p-3 text-xs">
      {steps.length ? (
        <ol className="grid gap-1.5 sm:grid-cols-3" aria-label={t("desktop.tasks.truthfulProgress")}>
          {steps.map((step) => (
            <li key={step.stage} className="rounded-md border border-border/60 bg-background/60 px-2 py-1.5">
              <span className="block font-medium">{t(`desktop.knowledgeBases.importStages.${step.stage}`)}</span>
              <span className="text-muted-foreground">
                {t(`desktop.knowledgeBases.importStatuses.${step.status}`)}
                {step.total !== undefined ? ` · ${step.completed ?? 0}/${step.total}` : ""}
              </span>
              {step.runtimeKind === "parser" && step.parserRoute && step.parserRuntimeState ? (
                <span className="mt-1 block text-muted-foreground">
                  {t("desktop.tasks.parserDetail", {
                    family: t(`desktop.tasks.parserFamilies.${step.parserFamily ?? "text"}`),
                    route: t(`desktop.tasks.parserRoutes.${step.parserRoute}`),
                    state: t(`desktop.engine.parserStates.${step.parserRuntimeState}`),
                  })}
                </span>
              ) : null}
              {step.errorCode ? <code className="mt-1 block text-destructive">{step.errorCode}</code> : null}
            </li>
          ))}
        </ol>
      ) : null}
      {activity ? (
        <div className="mt-3">
          <DesktopModelActivityDetails activity={activity} />
        </div>
      ) : null}
      {usage ? (
        <p className="mt-2 text-muted-foreground">
          {t("desktop.tasks.modelUsageSummary", {
            calls: usage.callCount,
            attempts: usage.attemptCount,
            tokens: usage.totalTokens.toLocaleString(),
            cost: usage.totalCost === null ? "—" : usage.totalCost.toFixed(4),
            source: usage.tokenUsageSource
              ? t(`desktop.tasks.tokenSources.${usage.tokenUsageSource}`)
              : t("desktop.tasks.tokenSources.unavailable"),
          })}
        </p>
      ) : null}
      {records.length ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-muted-foreground">
            {t("desktop.tasks.perCallUsage", { count: records.length })}
          </summary>
          <ul className="mt-2 space-y-1.5">
            {records.map((record) => (
              <li key={record.attemptId} className="rounded-md border border-border/60 bg-background/60 px-2 py-1.5 text-muted-foreground">
                <span className="font-medium text-foreground">{record.operation} · {record.model}</span>
                <span className="mt-0.5 block break-all font-mono">{record.callId} · {record.attemptId}</span>
                <span className="mt-0.5 block">
                  {t("desktop.tasks.perCallUsageRecord", {
                    tokens: record.totalTokens === null ? "—" : record.totalTokens.toLocaleString(),
                    source: record.tokenUsageSource
                      ? t(`desktop.tasks.tokenSources.${record.tokenUsageSource}`)
                      : t("desktop.tasks.tokenSources.unavailable"),
                    cost: record.totalCost === null ? "—" : record.totalCost.toFixed(4),
                  })}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  )
}
