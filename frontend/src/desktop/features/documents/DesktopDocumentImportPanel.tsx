import {
  CheckCircle2,
  CircleAlert,
  FilePlus2,
  FolderOpen,
  Loader2,
  Search,
  Upload,
  X,
  RefreshCw,
} from "lucide-react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { cn } from "@/lib/utils"
import type {
  DesktopImportSourceInspection,
  DesktopImportSourcePicker,
  DesktopImportTask,
} from "@/desktop/bridge/contracts"
import { currentDocumentTasks, taskIsFailed } from "@/desktop/features/documents/desktop-document-tasks"
import { DesktopKnowledgeAnalysisProgress } from "@/desktop/features/tasks/DesktopKnowledgeAnalysisProgress"
import { DesktopImportProgress } from "@/desktop/features/tasks/DesktopImportProgress"
import { DesktopModelResultDetails } from "@/desktop/features/tasks/DesktopModelResultDetails"
import type { KnowledgeReanalysisController } from "@/desktop/features/tasks/useKnowledgeReanalysis"

export interface DesktopImportBatchSummary {
  total: number
  completed: number
  failures: Array<{ name: string; reason: string }>
  running: boolean
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
  knowledgeReanalysis,
  onManualPathChange,
  onAddManualPath,
  onChooseSources,
  onRemoveSource,
  onSubmit,
  onControl,
  onOpenOriginal,
  onOpenFailedDocuments,
  requestedDocumentId,
  requestKey = 0,
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
  knowledgeReanalysis: KnowledgeReanalysisController
  onManualPathChange: (value: string) => void
  onAddManualPath: () => void
  onChooseSources: (picker: DesktopImportSourcePicker) => void
  onRemoveSource: (path: string) => void
  onSubmit: () => void
  onControl: (jobId: string, action: ImportTaskAction) => void
  onOpenOriginal: (documentId: string) => void
  onOpenFailedDocuments: () => void
  requestedDocumentId?: string | null
  requestKey?: number
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
      <DocumentList
        key={requestKey}
        tasks={tasks}
        controllingJobId={controllingJobId}
        knowledgeReanalysis={knowledgeReanalysis}
        onControl={onControl}
        onOpenOriginal={onOpenOriginal}
        onOpenFailedDocuments={onOpenFailedDocuments}
        requestedDocumentId={requestedDocumentId}
      />
    </section>
  )
}

function DocumentList({
  tasks,
  controllingJobId,
  knowledgeReanalysis,
  onControl,
  onOpenOriginal,
  onOpenFailedDocuments,
  requestedDocumentId,
}: {
  tasks: DesktopImportTask[]
  controllingJobId: string | null
  knowledgeReanalysis: KnowledgeReanalysisController
  onControl: (jobId: string, action: ImportTaskAction) => void
  onOpenOriginal: (documentId: string) => void
  onOpenFailedDocuments: () => void
  requestedDocumentId?: string | null
}) {
  const { t } = useTranslation("common")
  const [query, setQuery] = useState("")
  const [status, setStatus] = useState<"all" | "available" | "processing" | "quarantined">("all")
  const documents = currentDocumentTasks(tasks)
  const [selectedJobId, setSelectedJobId] = useState<string | null>(() => (
    documents.find((task) => task.document?.documentId === requestedDocumentId)?.job.jobId ?? null
  ))
  const selected = documents.find((task) => task.job.jobId === selectedJobId) ?? null
  const normalizedQuery = query.trim().toLowerCase()
  const analysisByDocument = new Map(
    knowledgeReanalysis.overview.documents.map((item) => [item.documentId, item]),
  )
  const activeAnalysisDocuments = new Set(knowledgeReanalysis.overview.runs.flatMap((run) => (
    run.jobs.filter((job) => ["pending", "running"].includes(job.status)).map((job) => job.documentId)
  )))
  const filtered = documents.filter((task) => {
    const matchesQuery = !normalizedQuery
      || task.job.sourceName.toLowerCase().includes(normalizedQuery)
      || task.document?.name.toLowerCase().includes(normalizedQuery)
    const matchesStatus = status === "all"
      || (status === "available" && task.document?.availability === "available")
      || (status === "processing" && ["running", "paused", "recoverable"].includes(task.job.status))
      || (status === "quarantined" && taskIsFailed(task))
    return matchesQuery && matchesStatus
  })

  return (
    <>
      <div className="mt-6 flex flex-col gap-3 border-t border-border/70 pt-5 sm:flex-row sm:items-center">
        <label className="relative min-w-0 flex-1">
          <span className="sr-only">{t("desktop.documents.search")}</span>
          <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("desktop.documents.search")} className="h-9 w-full rounded-md border border-input bg-background pl-9 pr-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </label>
        <select value={status} onChange={(event) => setStatus(event.target.value as typeof status)} className="h-9 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <option value="all">{t("desktop.documents.filters.all")}</option>
          <option value="available">{t("desktop.documents.filters.available")}</option>
          <option value="processing">{t("desktop.documents.filters.processing")}</option>
          <option value="quarantined">{t("desktop.documents.filters.quarantined")}</option>
        </select>
        <Button type="button" variant="outline" size="sm" onClick={onOpenFailedDocuments}>
          <CircleAlert className="size-4" />{t("desktop.knowledgeBases.failedDocuments")}
        </Button>
      </div>
      <div className="mt-3 overflow-hidden rounded-xl border border-border/70">
        {filtered.length ? (
          <ul className="divide-y divide-border/70">
            {filtered.map((task) => {
              const statusKey = task.document?.availability === "failed" ? "quarantined" : task.job.status
              const analysis = task.document ? analysisByDocument.get(task.document.documentId) : undefined
              return (
              <li key={task.document?.documentId ?? task.job.jobId}>
                <button type="button" onClick={() => setSelectedJobId(task.job.jobId)} className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-4 px-4 py-3 text-left outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
                  <span className="min-w-0"><strong className="block truncate text-sm font-medium">{task.document?.name ?? task.job.sourceName}</strong><span className="mt-0.5 block truncate text-xs text-muted-foreground">{task.job.sourceName}</span></span>
                  <span className="text-right">
                    <span className="block text-xs font-medium">{t(`desktop.knowledgeBases.importStatuses.${statusKey}`)}</span>
                    {analysis ? <span className="mt-0.5 block text-xs text-muted-foreground">{t(`desktop.documents.analysis.states.${analysis.state}`)}</span> : <span className="mt-0.5 block text-xs text-muted-foreground">{task.job.progress}%</span>}
                  </span>
                </button>
              </li>
              )
            })}
          </ul>
        ) : <p className="px-4 py-8 text-center text-sm text-muted-foreground">{t("desktop.documents.empty")}</p>}
      </div>
      <Sheet open={selected !== null} onOpenChange={(open) => { if (!open) setSelectedJobId(null) }}>
        <SheetContent className="w-[min(32rem,100vw)] overflow-y-auto sm:max-w-lg">
          <SheetHeader>
            <SheetTitle>{selected?.document?.name ?? selected?.job.sourceName}</SheetTitle>
            <SheetDescription>{t("desktop.documents.details")}</SheetDescription>
          </SheetHeader>
          {selected ? <>
            {selected.document?.availability === "available" ? (
              <DocumentAnalysisCard
                analysis={analysisByDocument.get(selected.document.documentId)}
                active={activeAnalysisDocuments.has(selected.document.documentId)}
                working={knowledgeReanalysis.workingId !== null}
                error={knowledgeReanalysis.error}
                onReanalyse={() => void knowledgeReanalysis.start([selected.document!.documentId])}
              />
            ) : null}
            <ImportTaskCard className="mt-4" task={selected} controlling={controllingJobId === selected.job.jobId} onControl={onControl} onOpenOriginal={onOpenOriginal} />
          </> : null}
        </SheetContent>
      </Sheet>
    </>
  )
}

function DocumentAnalysisCard({
  analysis,
  active,
  working,
  error,
  onReanalyse,
}: {
  analysis: KnowledgeReanalysisController["overview"]["documents"][number] | undefined
  active: boolean
  working: boolean
  error: string | null
  onReanalyse: () => void
}) {
  const { t } = useTranslation("common")
  return (
    <section className="mt-5 rounded-xl border border-border/70 bg-muted/20 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium">{t("desktop.documents.analysis.title")}</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            {analysis
              ? t(`desktop.documents.analysis.states.${analysis.state}`)
              : t("desktop.documents.analysis.states.missing")}
          </p>
        </div>
        <Button size="sm" variant="outline" disabled={active || working} onClick={onReanalyse}>
          {active ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
          {active ? t("desktop.documents.analysis.running") : t("desktop.documents.analysis.action")}
        </Button>
      </div>
      {analysis ? (
        <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
          <dt className="text-muted-foreground">{t("desktop.documents.analysis.schema")}</dt>
          <dd className="truncate text-right">{analysis.schemaVersion ?? "—"}</dd>
          <dt className="text-muted-foreground">{t("desktop.documents.analysis.model")}</dt>
          <dd className="truncate text-right">{analysis.provider && analysis.model ? `${analysis.provider}/${analysis.model}` : "—"}</dd>
          <dt className="text-muted-foreground">{t("desktop.documents.analysis.engine")}</dt>
          <dd className="truncate text-right">{analysis.engineVersion ?? "—"}</dd>
          <dt className="text-muted-foreground">{t("desktop.documents.analysis.analyzedAt")}</dt>
          <dd className="truncate text-right">{analysis.analyzedAt ? new Date(analysis.analyzedAt).toLocaleString() : "—"}</dd>
        </dl>
      ) : null}
      {error ? <p className="mt-3 text-xs text-destructive" role="alert">{error}</p> : null}
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
  const active = Math.max(0, summary.total - summary.completed - summary.failures.length)
  return (
    <section className="mt-5 rounded-xl border border-border/70 bg-muted/20 p-4" aria-live="polite">
      <div className="flex items-center gap-2">
        {summary.running ? <Loader2 className="size-4 animate-spin text-primary" /> : <CheckCircle2 className="size-4 text-emerald-600 dark:text-emerald-400" />}
        <h3 className="font-medium">
          {summary.running
            ? t("desktop.tasks.batchSummary", { total: summary.total, completed: summary.completed, active, failed: summary.failures.length })
            : t("desktop.knowledgeBases.importBatchComplete", { count: summary.completed })}
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
  const [cancellationWarning, setCancellationWarning] = useState(false)
  const stage = task.stages.find((item) => ["failed", "paused", "cancelled"].includes(item.status))
    ?? task.stages.find((item) => item.status === "running")
    ?? task.stages.at(-1)
  if (!stage) return null
  const jobStatus = task.job.status
  const sourceFailed = task.document?.availability === "failed"
  const currentStatus = sourceFailed ? "quarantined" : jobStatus
  const modelCall = task.modelCalls.at(-1)
  const quarantine = task.quarantine
  const modelCallIsWaiting = modelCall?.status === "running" || modelCall?.status === "retry_wait"
  return (
    <div className={cn("rounded-xl border border-border/70 bg-muted/30 p-4", className)}>
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="font-medium">{t("desktop.knowledgeBases.taskCenter")}</span>
        <span className="text-muted-foreground">{t(`desktop.knowledgeBases.importStatuses.${currentStatus}`)}</span>
      </div>
      <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted-foreground">{t("desktop.documents.source")}</dt>
        <dd className="truncate text-right" title={task.job.sourceName}>{task.job.sourceName}</dd>
        {task.document ? <>
          <dt className="text-muted-foreground">{t("desktop.documents.format")}</dt>
          <dd className="text-right uppercase">{task.document.sourceFormat}</dd>
          <dt className="text-muted-foreground">{t("desktop.documents.evidenceUnits")}</dt>
          <dd className="text-right">{task.document.evidenceCount}</dd>
          <dt className="text-muted-foreground">{t("desktop.documents.assetHash")}</dt>
          <dd className="truncate text-right font-mono" title={task.document.rawAssetSha256}>{task.document.rawAssetSha256.slice(0, 12)}…</dd>
        </> : null}
      </dl>
      {sourceFailed ? <p className="mt-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{t("desktop.documents.sourceUnavailable")}</p> : null}
      <p className="mt-3 text-xs font-medium text-muted-foreground">{t("desktop.documents.importHistory")}</p>
      <p className="mt-1 text-sm text-muted-foreground">
        {t("desktop.knowledgeBases.stageStatus", {
          stage: t(`desktop.knowledgeBases.importStages.${stage.stage}`),
          status: t(`desktop.knowledgeBases.importStatuses.${stage.status}`),
        })} · {stage.progress}%
      </p>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${task.job.progress}%` }} />
      </div>
      <DesktopImportProgress steps={task.importProgress} activity={task.modelActivity} usage={task.modelUsageAggregate} records={task.modelUsage} />
      {task.knowledgeAnalysis ? (
        <DesktopKnowledgeAnalysisProgress progress={task.knowledgeAnalysis} />
      ) : null}
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
              maximum: 3,
            })}
          </p>
          {modelCallIsWaiting ? (
            <p className="mt-1 text-muted-foreground">
              {t("desktop.knowledgeBases.modelCallElapsed", {
                elapsed: Math.floor(modelCall.elapsedSeconds),
              })}
            </p>
          ) : null}
          {modelCall.status === "retry_wait" ? (
            <p className="mt-1 text-muted-foreground">
              {t("desktop.knowledgeBases.modelRetrying", {
                reason: modelCall.reason,
              })}
            </p>
          ) : null}
          <DesktopModelResultDetails result={modelCall} />
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
          <Button size="sm" variant="ghost" disabled={controlling} onClick={() => {
            setCancellationWarning(true)
            onControl(task.job.jobId, "cancel")
          }}>
            {t("desktop.knowledgeBases.cancelImport")}
          </Button>
        ) : null}
      </div>
      {cancellationWarning ? (
        <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">
          {t("desktop.knowledgeBases.importCancellationWarning")}
        </p>
      ) : null}
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
