import { AlertTriangle, Loader2 } from "lucide-react"
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { DesktopImportTask, DesktopRecoveryOverride } from "./contracts"

interface FailedDocumentsDialogProps {
  open: boolean
  tasks: DesktopImportTask[]
  recoveringJobId: string | null
  onOpenChange: (open: boolean) => void
  onRecover: (jobId: string, override: DesktopRecoveryOverride) => void
}

/** Persistent failure-menu details and one-time recovery controls. */
export function FailedDocumentsDialog({
  open,
  tasks,
  recoveringJobId,
  onOpenChange,
  onRecover,
}: FailedDocumentsDialogProps) {
  const { t } = useTranslation("common")
  const failedTasks = useMemo(
    () => tasks.filter((task) => task.job.status === "quarantined" && task.quarantine !== null),
    [tasks],
  )
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)
  const [model, setModel] = useState("")
  const [timeout, setTimeout] = useState("")
  const [formError, setFormError] = useState<string | null>(null)

  const selected = failedTasks.find((task) => task.job.jobId === selectedJobId) ?? failedTasks[0] ?? null
  const attempts = selected?.modelCalls.flatMap((call) => call.attempts) ?? []

  const chooseTask = (jobId: string) => {
    setSelectedJobId(jobId)
    setModel("")
    setTimeout("")
    setFormError(null)
  }

  const recover = () => {
    if (selected === null) return
    const timeoutValue = timeout.trim()
    const parsedTimeout = timeoutValue ? Number(timeoutValue) : undefined
    if (
      parsedTimeout !== undefined
      && (!Number.isFinite(parsedTimeout) || parsedTimeout <= 0 || parsedTimeout > 60)
    ) {
      setFormError(t("desktop.knowledgeBases.recoveryInvalidTimeout"))
      return
    }
    setFormError(null)
    onRecover(selected.job.jobId, {
      model: model.trim() || undefined,
      initialTimeoutSeconds: parsedTimeout,
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[min(44rem,calc(100vh-2rem))] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t("desktop.knowledgeBases.failedDocuments")}</DialogTitle>
          <DialogDescription>{t("desktop.knowledgeBases.failedDocumentsDescription")}</DialogDescription>
        </DialogHeader>
        {failedTasks.length === 0 ? (
          <div className="rounded-lg border border-border/70 bg-muted/30 p-5 text-sm text-muted-foreground">
            {t("desktop.knowledgeBases.failedDocumentsEmpty")}
          </div>
        ) : (
          <div className="grid gap-5 md:grid-cols-[12rem_minmax(0,1fr)]">
            <div className="space-y-2">
              {failedTasks.map((task) => (
                <button
                  key={task.job.jobId}
                  type="button"
                  onClick={() => chooseTask(task.job.jobId)}
                  className={`w-full rounded-lg border p-3 text-left text-sm transition-colors ${
                    task.job.jobId === selected?.job.jobId
                      ? "border-primary bg-primary/10"
                      : "border-border/70 bg-muted/20 hover:bg-muted/50"
                  }`}
                >
                  <span className="block truncate font-medium">{task.job.sourceName}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">
                    {t("desktop.knowledgeBases.quarantinedAttempts", {
                      attempts: task.quarantine?.attemptCount ?? 0,
                    })}
                  </span>
                </button>
              ))}
            </div>
            {selected?.quarantine ? (
              <div className="min-w-0 space-y-5">
                <div className="rounded-lg border border-destructive/35 bg-destructive/5 p-4 text-sm">
                  <div className="flex items-start gap-2">
                    <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
                    <div>
                      <p className="font-medium">{selected.job.sourceName}</p>
                      <p className="mt-2 text-muted-foreground">
                        {t("desktop.knowledgeBases.failureStage", {
                          stage: t(`desktop.knowledgeBases.importStages.${selected.quarantine.stage}`),
                        })}
                      </p>
                      <p className="mt-2 text-muted-foreground">{selected.quarantine.reason}</p>
                      <p className="mt-2 text-muted-foreground">{selected.quarantine.suggestedAction}</p>
                    </div>
                  </div>
                </div>

                <div>
                  <h3 className="text-sm font-medium">{t("desktop.knowledgeBases.attemptHistory")}</h3>
                  <div className="mt-2 space-y-2">
                    {attempts.map((attempt, index) => (
                      <div
                        key={`${attempt.attempt}-${index}`}
                        className="rounded-md border border-border/70 bg-muted/20 px-3 py-2 text-sm"
                      >
                        <p>
                          {t("desktop.knowledgeBases.attemptRecord", {
                            attempt: attempt.attempt,
                            status: t(`desktop.knowledgeBases.modelCallStates.${attempt.status}`),
                            timeout: Math.ceil(attempt.timeoutSeconds),
                          })}
                        </p>
                        {attempt.reason ? (
                          <p className="mt-1 text-xs text-muted-foreground">{attempt.reason}</p>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </div>

                <div className="rounded-lg border border-border/70 p-4">
                  <h3 className="font-medium">{t("desktop.knowledgeBases.recoveryTitle")}</h3>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">
                    {t("desktop.knowledgeBases.recoveryDescription")}
                  </p>
                  <div className="mt-4 grid gap-3 sm:grid-cols-2">
                    <label className="text-sm font-medium" htmlFor="desktop-recovery-model">
                      {t("desktop.knowledgeBases.recoveryModel")}
                      <input
                        id="desktop-recovery-model"
                        value={model}
                        onChange={(event) => setModel(event.target.value)}
                        placeholder={t("desktop.knowledgeBases.recoveryModelPlaceholder")}
                        className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 text-sm font-normal outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      />
                    </label>
                    <label className="text-sm font-medium" htmlFor="desktop-recovery-timeout">
                      {t("desktop.knowledgeBases.recoveryTimeout")}
                      <input
                        id="desktop-recovery-timeout"
                        type="number"
                        min="1"
                        max="60"
                        step="1"
                        value={timeout}
                        onChange={(event) => setTimeout(event.target.value)}
                        placeholder={t("desktop.knowledgeBases.recoveryTimeoutPlaceholder")}
                        className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 text-sm font-normal outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      />
                    </label>
                  </div>
                  {formError ? <p className="mt-3 text-sm text-destructive" role="alert">{formError}</p> : null}
                  <DialogFooter className="mt-4">
                    <Button
                      type="button"
                      disabled={recoveringJobId === selected.job.jobId}
                      onClick={recover}
                    >
                      {recoveringJobId === selected.job.jobId ? <Loader2 className="size-4 animate-spin" /> : null}
                      {t("desktop.knowledgeBases.recoverImport")}
                    </Button>
                  </DialogFooter>
                </div>
              </div>
            ) : null}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
