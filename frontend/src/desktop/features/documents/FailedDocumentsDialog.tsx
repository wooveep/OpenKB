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
import type { DesktopImportTask, DesktopRecoveryOverride } from "@/desktop/bridge/contracts"
import { DesktopModelResultDetails } from "@/desktop/features/tasks/DesktopModelResultDetails"

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
  const [contextCapacity, setContextCapacity] = useState("")
  const [parserMode, setParserMode] = useState<"" | "auto" | "fast" | "enhanced">("")
  const [reasoning, setReasoning] = useState<"" | "off" | "low" | "medium" | "high">("")
  const [legacyChoice, setLegacyChoice] = useState<"continue_compatible" | "restart_current_plan" | null>(null)
  const [formError, setFormError] = useState<string | null>(null)

  const selected = failedTasks.find((task) => task.job.jobId === selectedJobId) ?? failedTasks[0] ?? null
  const attempts = selected?.modelCalls.flatMap((call) => call.attempts) ?? []

  const chooseTask = (jobId: string) => {
    setSelectedJobId(jobId)
    setModel("")
    setContextCapacity("")
    setParserMode("")
    setReasoning("")
    setLegacyChoice(failedTasks.find((task) => task.job.jobId === jobId)?.legacyModelRecovery?.recommendedChoice ?? null)
    setFormError(null)
  }

  const recover = () => {
    if (selected === null) return
    const contextValue = contextCapacity.trim()
    const parsedContext = contextValue ? Number(contextValue) : undefined
    if (
      parsedContext !== undefined
      && (!Number.isInteger(parsedContext) || parsedContext < 4096)
    ) {
      setFormError(t("desktop.knowledgeBases.recoveryInvalidContext"))
      return
    }
    const selectedChoice = legacyChoice ?? selected.legacyModelRecovery?.recommendedChoice
    if (selected.legacyModelRecovery && !selectedChoice) {
      setFormError(t("desktop.knowledgeBases.recoveryChoiceRequired"))
      return
    }
    setFormError(null)
    onRecover(selected.job.jobId, {
      model: model.trim() || undefined,
      contextCapacity: parsedContext,
      reasoning: reasoning || undefined,
      legacyRecoveryChoice: selectedChoice,
      checkAndRecover: true,
      parserMode: parserMode || undefined,
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
                            elapsed: Math.floor(attempt.elapsedSeconds),
                          })}
                        </p>
                        {attempt.reason ? (
                          <p className="mt-1 text-xs text-muted-foreground">{attempt.reason}</p>
                        ) : null}
                        <DesktopModelResultDetails result={attempt} />
                      </div>
                    ))}
                  </div>
                </div>

                <div className="rounded-lg border border-border/70 p-4">
                  <h3 className="font-medium">{t("desktop.knowledgeBases.recoveryTitle")}</h3>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">
                    {t("desktop.knowledgeBases.recoveryDescription")}
                  </p>
                  <div className="mt-4 grid gap-3 sm:grid-cols-3">
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
                    <label className="text-sm font-medium" htmlFor="desktop-recovery-context">
                      {t("desktop.knowledgeBases.recoveryContext")}
                      <input
                        id="desktop-recovery-context"
                        type="number"
                        min="4096"
                        step="1024"
                        value={contextCapacity}
                        onChange={(event) => setContextCapacity(event.target.value)}
                        placeholder={t("desktop.knowledgeBases.recoveryContextPlaceholder")}
                        className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 text-sm font-normal outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      />
                    </label>
                    <label className="text-sm font-medium" htmlFor="desktop-recovery-reasoning">
                      {t("desktop.knowledgeBases.recoveryReasoning")}
                      <select
                        id="desktop-recovery-reasoning"
                        value={reasoning}
                        onChange={(event) => setReasoning(event.target.value as typeof reasoning)}
                        className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 text-sm font-normal outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <option value="">{t("desktop.knowledgeBases.recoveryReasoningConfigured")}</option>
                        {(["off", "low", "medium", "high"] as const).map((value) => (
                          <option key={value} value={value}>
                            {t(`desktop.knowledgeBases.modelSettings.reasoning.${value}`)}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <label className="mt-4 block text-sm font-medium" htmlFor="desktop-recovery-parser">
                    {t("desktop.knowledgeBases.recoveryParser")}
                    <select id="desktop-recovery-parser" value={parserMode}
                      onChange={(event) => setParserMode(event.target.value as typeof parserMode)}
                      className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 text-sm font-normal">
                      {(["", "auto", "fast", "enhanced"] as const).map((value) => (
                        <option key={value} value={value}>{t(`desktop.knowledgeBases.recoveryParserModes.${value || "keep"}`)}</option>
                      ))}
                    </select>
                    {parserMode ? <span className="mt-2 block text-xs font-normal text-muted-foreground">{t("desktop.knowledgeBases.recoveryReparseNotice")}</span> : null}
                  </label>
                  {selected.legacyModelRecovery ? (
                    <fieldset className="mt-4 space-y-2">
                      <legend className="text-sm font-medium">
                        {t(selected.legacyModelRecovery.kind === "model_execution_profile_replan"
                          ? "desktop.knowledgeBases.recoveryProfileChoiceTitle"
                          : "desktop.knowledgeBases.recoveryChoiceTitle")}
                      </legend>
                      {selected.legacyModelRecovery.kind === "model_execution_profile_replan" ? (
                        <p className="rounded-md border border-amber-500/35 bg-amber-500/10 px-3 py-2 text-xs leading-5 text-muted-foreground">
                          {t("desktop.knowledgeBases.recoveryProfileReplanNotice", {
                            checkpoints: selected.legacyModelRecovery.discardedModelCheckpoints,
                          })}
                        </p>
                      ) : null}
                      {(["continue_compatible", "restart_current_plan"] as const).map((choice) => {
                        const estimate = selected.legacyModelRecovery!.choices[choice]
                        const checked = (legacyChoice ?? selected.legacyModelRecovery!.recommendedChoice) === choice
                        return (
                          <label key={choice} className={`block rounded-lg border p-3 text-sm ${checked ? "border-primary bg-primary/5" : "border-border/70"} ${estimate.allowed ? "cursor-pointer" : "opacity-60"}`}>
                            <span className="flex items-start gap-2">
                              <input type="radio" name="legacy-recovery-choice" value={choice} checked={checked} disabled={!estimate.allowed} onChange={() => setLegacyChoice(choice)} />
                              <span>
                                <span className="font-medium">{t(`desktop.knowledgeBases.recoveryChoices.${choice}.title`)}</span>
                                {selected.legacyModelRecovery!.recommendedChoice === choice ? <span className="ml-2 text-xs text-primary">{t("desktop.knowledgeBases.recoveryRecommended")}</span> : null}
                                <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                                  {t(`desktop.knowledgeBases.recoveryChoices.${choice}.description`, {
                                    calls: estimate.estimatedRemainingCalls,
                                    tokens: estimate.estimatedInputTokens.toLocaleString(),
                                    batches: estimate.reusesCompletedBatches ?? 0,
                                  })}
                                </span>
                              </span>
                            </span>
                          </label>
                        )
                      })}
                      <p className="text-xs leading-5 text-muted-foreground">{selected.legacyModelRecovery.compatibilityReason}</p>
                    </fieldset>
                  ) : null}
                  {formError ? <p className="mt-3 text-sm text-destructive" role="alert">{formError}</p> : null}
                  <DialogFooter className="mt-4">
                    <Button
                      type="button"
                      disabled={recoveringJobId === selected.job.jobId}
                      onClick={recover}
                    >
                      {recoveringJobId === selected.job.jobId ? <Loader2 className="size-4 animate-spin" /> : null}
                      {t(recoveringJobId === selected.job.jobId
                        ? "desktop.knowledgeBases.checkingAndRecovering"
                        : "desktop.knowledgeBases.checkAndRecover")}
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
