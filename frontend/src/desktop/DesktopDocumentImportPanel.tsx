import {
  CheckCircle2,
  CircleAlert,
  FilePlus2,
  FolderOpen,
  Loader2,
  Upload,
  X,
} from "lucide-react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type {
  DesktopImportSourceInspection,
  DesktopImportSourcePicker,
  DesktopImportTask,
} from "./contracts"

export interface DesktopImportBatchSummary {
  completed: number
  failures: Array<{ name: string; reason: string }>
}

type ImportTaskAction = "pause" | "resume" | "cancel"

/** Select, inspect, and independently run a local Desktop import batch. */
export function DesktopDocumentImportPanel({
  error,
  importing,
  manualPath,
  sources,
  inspection,
  inspecting,
  dropActive,
  summary,
  tasks,
  controllingJobId,
  onManualPathChange,
  onAddManualPath,
  onChooseSources,
  onRemoveSource,
  onSubmit,
  onControl,
  onOpenOriginal,
}: {
  error: string | null
  importing: boolean
  manualPath: string
  sources: string[]
  inspection: DesktopImportSourceInspection | null
  inspecting: boolean
  dropActive: boolean
  summary: DesktopImportBatchSummary | null
  tasks: DesktopImportTask[]
  controllingJobId: string | null
  onManualPathChange: (value: string) => void
  onAddManualPath: () => void
  onChooseSources: (picker: DesktopImportSourcePicker) => void
  onRemoveSource: (path: string) => void
  onSubmit: () => void
  onControl: (jobId: string, action: ImportTaskAction) => void
  onOpenOriginal: (documentId: string) => void
}) {
  const { t } = useTranslation("common")
  const supportedCount = inspection?.supported.length ?? 0
  const hasLegacyOfficeSource = inspection?.supported.some((source) => /\.(doc|ppt)$/i.test(source.name))
  return (
    <section className="mt-6 rounded-apple-lg border border-border/70 bg-background p-6">
      <div className="flex items-start gap-3">
        <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary">
          <Upload className="size-4" />
        </div>
        <div>
          <h2 className="font-semibold">{t("desktop.knowledgeBases.importTitle")}</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            {t("desktop.knowledgeBases.importDescription")}
          </p>
        </div>
      </div>

      <div
        className={cn(
          "mt-5 rounded-xl border border-dashed p-5 transition-colors",
          dropActive
            ? "border-primary bg-primary/5"
            : "border-border/80 bg-muted/20",
        )}
        data-testid="desktop-import-drop-zone"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={importing}
            onClick={() => onChooseSources("files")}
          >
            <FilePlus2 className="size-4" />
            {t("desktop.knowledgeBases.chooseFiles")}
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={importing}
            onClick={() => onChooseSources("directory")}
          >
            <FolderOpen className="size-4" />
            {t("desktop.knowledgeBases.chooseFolder")}
          </Button>
          <p className="text-sm text-muted-foreground">{t("desktop.knowledgeBases.dropSources")}</p>
        </div>
        <form
          className="mt-4 flex flex-col gap-2 sm:flex-row"
          onSubmit={(event) => {
            event.preventDefault()
            onAddManualPath()
          }}
        >
          <label className="sr-only" htmlFor="desktop-import-source-path">
            {t("desktop.knowledgeBases.importPathLabel")}
          </label>
          <input
            id="desktop-import-source-path"
            value={manualPath}
            disabled={importing}
            onChange={(event) => onManualPathChange(event.target.value)}
            placeholder={t("desktop.knowledgeBases.importPathPlaceholder")}
            className="h-10 min-w-0 flex-1 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <Button type="submit" variant="outline" disabled={importing}>
            {t("desktop.knowledgeBases.addSource")}
          </Button>
        </form>
      </div>

      {error ? <p className="mt-3 text-sm text-destructive" role="alert">{error}</p> : null}
      {inspecting ? (
        <p className="mt-4 flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          {t("desktop.knowledgeBases.inspectingSources")}
        </p>
      ) : null}
      {inspection ? (
        <div className="mt-5 space-y-4 rounded-xl border border-border/70 bg-muted/20 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 className="font-medium">{t("desktop.knowledgeBases.importPreview")}</h3>
              <p className="mt-1 text-sm text-muted-foreground">
                {t("desktop.knowledgeBases.supportedFormats", {
                  formats: inspection.supportedExtensions.join(", "),
                })}
              </p>
              {hasLegacyOfficeSource ? (
                <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">
                  {t("desktop.knowledgeBases.legacyOfficeNotice")}
                </p>
              ) : null}
            </div>
            <Button disabled={importing || supportedCount === 0} onClick={onSubmit}>
              {importing ? <Loader2 className="size-4 animate-spin" /> : <Upload className="size-4" />}
              {t("desktop.knowledgeBases.importBatchAction", { count: supportedCount })}
            </Button>
          </div>
          <ImportSourceList
            title={t("desktop.knowledgeBases.importableSources", { count: supportedCount })}
            sources={inspection.supported}
            onRemove={onRemoveSource}
          />
          <ImportSourceList
            title={t("desktop.knowledgeBases.unprocessableSources", {
              count: inspection.unsupported.length,
            })}
            sources={inspection.unsupported}
            onRemove={onRemoveSource}
          />
        </div>
      ) : sources.length ? null : (
        <p className="mt-4 text-sm text-muted-foreground">{t("desktop.knowledgeBases.noSourcesSelected")}</p>
      )}

      {summary ? <ImportBatchSummary summary={summary} /> : null}
      {tasks.map((task) => (
        <ImportTaskCard
          key={task.job.jobId}
          className="mt-5"
          task={task}
          controlling={controllingJobId === task.job.jobId}
          onControl={onControl}
          onOpenOriginal={onOpenOriginal}
        />
      ))}
    </section>
  )
}

function ImportSourceList({
  title,
  sources,
  onRemove,
}: {
  title: string
  sources: DesktopImportSourceInspection["supported"]
  onRemove: (path: string) => void
}) {
  const { t } = useTranslation("common")
  if (!sources.length) return null
  return (
    <section>
      <h4 className="text-sm font-medium">{title}</h4>
      <ul className="mt-2 max-h-44 space-y-1 overflow-y-auto rounded-lg border border-border/60 bg-background p-2">
        {sources.map((source) => (
          <li key={source.path} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm">
            <span className="min-w-0 flex-1 truncate" title={source.path}>{source.name}</span>
            {source.errorCode ? (
              <span className="text-xs text-muted-foreground">
                {t(`desktop.knowledgeBases.importSourceErrors.${source.errorCode}`)}
              </span>
            ) : null}
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-7 shrink-0"
              aria-label={t("desktop.knowledgeBases.removeSource", { name: source.name })}
              onClick={() => onRemove(source.path)}
            >
              <X className="size-3.5" />
            </Button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function ImportBatchSummary({ summary }: { summary: DesktopImportBatchSummary }) {
  const { t } = useTranslation("common")
  return (
    <section className="mt-5 rounded-xl border border-border/70 bg-muted/20 p-4" aria-live="polite">
      <div className="flex items-center gap-2">
        <CheckCircle2 className="size-4 text-emerald-600 dark:text-emerald-400" />
        <h3 className="font-medium">
          {t("desktop.knowledgeBases.importBatchComplete", { count: summary.completed })}
        </h3>
      </div>
      {summary.failures.length ? (
        <div className="mt-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3">
          <p className="flex items-center gap-2 text-sm font-medium text-destructive">
            <CircleAlert className="size-4" />
            {t("desktop.knowledgeBases.importBatchFailures", { count: summary.failures.length })}
          </p>
          <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
            {summary.failures.map((failure) => (
              <li key={failure.name}>
                <span className="font-medium text-foreground">{failure.name}</span> · {failure.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

function ImportTaskCard({
  className,
  task,
  controlling = false,
  onControl,
  onOpenOriginal,
}: {
  className?: string
  task: DesktopImportTask
  controlling?: boolean
  onControl: (jobId: string, action: ImportTaskAction) => void
  onOpenOriginal: (documentId: string) => void
}) {
  const { t } = useTranslation("common")
  const stage = task.stages.find((item) => ["failed", "paused", "cancelled"].includes(item.status))
    ?? task.stages.find((item) => item.status === "running")
    ?? task.stages.at(-1)
  if (!stage) return null
  const jobStatus = task.job.status
  const modelCall = task.modelCalls.at(-1)
  const quarantine = task.quarantine
  const modelCallIsWaiting = modelCall?.status === "running" || modelCall?.status === "retry_wait"
  return (
    <div className={cn("rounded-xl border border-border/70 bg-muted/30 p-4", className)}>
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="font-medium">{t("desktop.knowledgeBases.taskCenter")}</span>
        <span className="text-muted-foreground">{task.job.progress}%</span>
      </div>
      <p className="mt-2 text-sm text-muted-foreground">
        {t("desktop.knowledgeBases.stageStatus", {
          stage: t(`desktop.knowledgeBases.importStages.${stage.stage}`),
          status: t(`desktop.knowledgeBases.importStatuses.${stage.status}`),
        })} · {stage.progress}%
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        {t(`desktop.knowledgeBases.importStatuses.${jobStatus}`)}
      </p>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${task.job.progress}%` }} />
      </div>
      {task.job.deduplication ? (
        <p className="mt-3 text-sm text-muted-foreground">
          {task.job.deduplication.level === "D0"
            ? t("desktop.knowledgeBases.deduplicationD0")
            : task.job.deduplication.level === "D1"
              ? t("desktop.knowledgeBases.deduplicationD1")
              : t("desktop.knowledgeBases.deduplicationD2", {
                count: task.job.deduplication.reusedEvidenceCount,
              })}
        </p>
      ) : null}
      {modelCall ? (
        <div className="mt-4 border-t border-border/70 pt-4 text-sm">
          <p className="text-muted-foreground">
            {t("desktop.knowledgeBases.modelCallStatus", {
              status: t(`desktop.knowledgeBases.modelCallStates.${modelCall.status}`),
              attempt: modelCall.attemptCount,
              maximum: 4,
            })}
          </p>
          {modelCallIsWaiting ? (
            <p className="mt-1 text-muted-foreground">
              {t("desktop.knowledgeBases.modelCallBudget", {
                timeout: Math.ceil(modelCall.timeoutSeconds),
                remaining: Math.ceil(modelCall.remainingSeconds),
              })}
            </p>
          ) : null}
          {modelCall.status === "retry_wait" && modelCall.nextTimeoutSeconds !== null ? (
            <p className="mt-1 text-muted-foreground">
              {t("desktop.knowledgeBases.modelRetrying", {
                reason: modelCall.reason,
                timeout: Math.ceil(modelCall.nextTimeoutSeconds),
              })}
            </p>
          ) : null}
        </div>
      ) : null}
      {quarantine ? (
        <div className="mt-4 rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-sm">
          <p className="font-medium text-destructive">{t("desktop.knowledgeBases.quarantinedTitle")}</p>
          <p className="mt-1 text-muted-foreground">{quarantine.reason}</p>
          <p className="mt-1 text-muted-foreground">{quarantine.suggestedAction}</p>
          <p className="mt-2 text-xs text-muted-foreground">
            {t("desktop.knowledgeBases.quarantinedAttempts", { attempts: quarantine.attemptCount })}
          </p>
        </div>
      ) : null}
      <div className="mt-4 flex flex-wrap gap-2 border-t border-border/70 pt-4">
        {task.job.status === "running" ? (
          <Button size="sm" variant="outline" disabled={controlling} onClick={() => onControl(task.job.jobId, "pause")}>
            {t("desktop.knowledgeBases.pauseImport")}
          </Button>
        ) : null}
        {task.job.status === "paused" || task.job.status === "recoverable" ? (
          <Button size="sm" disabled={controlling} onClick={() => onControl(task.job.jobId, "resume")}>
            {controlling ? <Loader2 className="size-4 animate-spin" /> : null}
            {t("desktop.knowledgeBases.resumeImport")}
          </Button>
        ) : null}
        {task.job.status === "running" || task.job.status === "paused" || task.job.status === "recoverable" ? (
          <Button size="sm" variant="ghost" disabled={controlling} onClick={() => onControl(task.job.jobId, "cancel")}>
            {t("desktop.knowledgeBases.cancelImport")}
          </Button>
        ) : null}
      </div>
      {task.document?.availability === "available" ? (
        <div className="mt-4 border-t border-border/70 pt-4 text-sm">
          <p className="font-medium text-emerald-700 dark:text-emerald-300">
            {t("desktop.knowledgeBases.availableKnowledge")}
          </p>
          <p className="mt-1 text-muted-foreground">
            {t("desktop.knowledgeBases.importedDocument", {
              name: task.document.name,
              evidence: task.document.evidenceCount,
            })}
          </p>
          <Button
            className="mt-3"
            size="sm"
            variant="outline"
            onClick={() => onOpenOriginal(task.document!.documentId)}
          >
            {t("desktop.knowledgeBases.openOriginal")}
          </Button>
        </div>
      ) : task.document ? (
        <div className="mt-4 rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-sm text-muted-foreground">
          {t("desktop.knowledgeBases.originalUnavailable")}
        </div>
      ) : null}
    </div>
  )
}
