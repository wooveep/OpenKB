import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import type { DesktopBridge, DesktopKnowledgeGraphExtractionTask } from "./contracts"
import { DesktopModelActivityDetails } from "./DesktopModelActivityDetails"

export function DesktopKnowledgeGraphExtractionTasks({
  tasks,
  bridge,
}: {
  tasks: DesktopKnowledgeGraphExtractionTask[]
  bridge: DesktopBridge
}) {
  const { t } = useTranslation("common")
  const [controllingDocumentId, setControllingDocumentId] = useState<string | null>(null)
  const [controlError, setControlError] = useState<string | null>(null)
  const [cancellationWarning, setCancellationWarning] = useState(false)

  const control = async (task: DesktopKnowledgeGraphExtractionTask, cancel: boolean) => {
    setControllingDocumentId(task.documentId)
    setControlError(null)
    try {
      const result = cancel
        ? await bridge.cancelKnowledgeGraphExtraction(task.documentId)
        : await bridge.retryKnowledgeGraphExtraction(task.documentId)
      if (!result.accepted) {
        setControlError(t("desktop.tasks.knowledgeGraph.actionRejected"))
      } else if (cancel) {
        setCancellationWarning(true)
      }
    } catch (reason) {
      setControlError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setControllingDocumentId(null)
    }
  }

  if (!tasks.length) return null
  return (
    <section className="mb-4 space-y-2" aria-label={t("desktop.tasks.knowledgeGraph.title")}>
      <div>
        <h3 className="text-sm font-semibold">{t("desktop.tasks.knowledgeGraph.title")}</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          {t("desktop.tasks.knowledgeGraph.description")}
        </p>
      </div>
      {tasks.map((task) => (
        <article key={task.documentId} className="rounded-xl border border-border/70 bg-muted/20 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{task.documentName}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {t(`desktop.tasks.knowledgeGraph.statuses.${task.status}`)}
                {" · "}{task.provider} / {task.model}
              </p>
              {task.status === "completed" || task.status === "completed_empty" ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  {t("desktop.tasks.knowledgeGraph.counts", {
                    nodes: task.nodeCount,
                    edges: task.edgeCount,
                  })}
                </p>
              ) : null}
            </div>
            <span className="text-xs text-muted-foreground">
              {t("desktop.tasks.knowledgeGraph.attempts", { count: task.attemptCount })}
            </span>
          </div>
          {task.modelActivity ? (
            <div className="mt-3 text-xs">
              <DesktopModelActivityDetails activity={task.modelActivity} />
            </div>
          ) : task.callId ? (
            <p className="mt-2 break-all font-mono text-xs text-muted-foreground">
              {t("desktop.tasks.knowledgeGraph.callIdentity", {
                callId: task.callId,
                attemptId: `${task.callId}:${task.modelAttempt}`,
              })}
            </p>
          ) : null}
          {task.errorReason || task.errorCode ? (
            <p className="mt-2 text-xs text-destructive">{task.errorReason ?? task.errorCode}</p>
          ) : null}
          {task.errorCode === "knowledge_graph_response_invalid" ? (
            <p className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-xs text-amber-800 dark:text-amber-200">
              {t("desktop.tasks.knowledgeGraph.invalidRecovery")}
            </p>
          ) : null}
          {task.status === "running" ? (
            <Button className="mt-3" size="sm" variant="outline" disabled={controllingDocumentId === task.documentId} onClick={() => void control(task, true)}>
              {t("desktop.tasks.knowledgeGraph.cancel")}
            </Button>
          ) : task.status === "pending" || task.status === "failed" ? (
            <Button className="mt-3" size="sm" disabled={controllingDocumentId === task.documentId} onClick={() => void control(task, false)}>
              {t(task.status === "failed" ? "desktop.tasks.knowledgeGraph.retry" : "desktop.tasks.knowledgeGraph.resume")}
            </Button>
          ) : null}
        </article>
      ))}
      {cancellationWarning ? (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          {t("desktop.tasks.knowledgeGraph.cancelWarning")}
        </p>
      ) : null}
      {controlError ? <p className="text-xs text-destructive">{controlError}</p> : null}
    </section>
  )
}
