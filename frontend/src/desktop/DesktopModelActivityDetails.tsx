import { AlertTriangle } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import type { DesktopModelActivity } from "./contracts"
import type {
  DesktopModelCallLifecycleEvent,
  DesktopModelCallLifecycleStatus,
} from "./model-call-lifecycle-contracts"

export type DesktopLiveModelActivity = DesktopModelCallLifecycleEvent["data"] & {
  observedAtMs: number
}

const activeLifecycleStatuses = new Set<DesktopModelCallLifecycleStatus>([
  "queued",
  "connecting",
  "awaiting_model_result",
  "model_output_activity",
  "validating",
  "retrying",
])

/** Render one sanitized, content-free Model Attempt identity and state. */
export function DesktopModelActivityDetails({ activity }: { activity: DesktopModelActivity }) {
  const { t } = useTranslation("common")
  return (
    <div className={`rounded-md border px-3 py-2 ${activity.longWaitAdvisory ? "border-amber-500/40 bg-amber-500/10" : "border-border/60 bg-background/60"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">
          {t(`desktop.tasks.modelActivityStates.${activity.status}`)}
        </span>
        <span className="text-muted-foreground">
          {t("desktop.tasks.modelElapsed", { seconds: Math.floor(activity.elapsedSeconds) })}
        </span>
      </div>
      <p className="mt-1 text-muted-foreground">
        {t("desktop.tasks.modelActivityIdentity", {
          operation: activity.operation,
          model: activity.model,
          role: t(`desktop.tasks.modelRoles.${activity.modelRole}`),
        })}
        {" · "}{t(`desktop.tasks.executionLanes.${activity.executionLane}`)}
        {activity.batchId ? ` · ${activity.batchId}` : ""}
      </p>
      <p className="mt-1 break-all font-mono text-muted-foreground">
        {t("desktop.tasks.modelActivityAttempt", {
          callId: activity.callId,
          attempt: activity.attempt,
          attemptId: activity.attemptId,
        })}
      </p>
      {activity.longWaitAdvisory ? (
        <p className="mt-2 flex items-start gap-1.5 text-amber-800 dark:text-amber-200">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          {t("desktop.tasks.longWaitAdvisory")}
        </p>
      ) : null}
    </div>
  )
}

/** Keep elapsed wait and the Long Wait Advisory truthful during silent live calls. */
export function DesktopLiveModelActivityDetails({
  activity,
}: {
  activity: DesktopLiveModelActivity
}) {
  const active = activeLifecycleStatuses.has(activity.status)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [active])
  const elapsedSeconds = activity.elapsedSeconds
    + (active ? Math.max(0, now - activity.observedAtMs) / 1_000 : 0)
  const status = lifecycleActivityStatus(activity.status)
  return (
    <DesktopModelActivityDetails
      activity={{
        operation: activity.operation,
        modelRole: activity.modelRole,
        provider: activity.provider,
        model: activity.modelName,
        callId: activity.callId,
        attempt: activity.attempt,
        attemptId: activity.attemptId,
        batchId: null,
        executionLane: activity.executionLane,
        status,
        failureCode: activity.failureCode,
        elapsedSeconds,
        longWaitAdvisory: active && elapsedSeconds >= activity.longWaitThresholdSeconds,
        longWaitThresholdSeconds: activity.longWaitThresholdSeconds,
        availableActions: active ? ["cancel"] : [],
      }}
    />
  )
}

function lifecycleActivityStatus(
  status: DesktopModelCallLifecycleStatus,
): DesktopModelActivity["status"] {
  if (status === "awaiting_model_result") return "awaiting_first_result"
  if (status === "model_output_activity") return "receiving_output"
  if (status === "cancelled") return "interrupted"
  return status
}
