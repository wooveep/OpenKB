import {
  BookOpen,
  CheckCircle2,
  ClipboardCheck,
  FileText,
  FolderOpen,
  LayoutDashboard,
  Loader2,
  MessageSquare,
  Plus,
  Settings,
  Upload,
} from "lucide-react"
import { useCallback, useEffect, useRef, useState, type ComponentType } from "react"
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
import { LanguageToggle } from "@/lib/language"
import { ThemeToggle } from "@/lib/theme"
import { cn } from "@/lib/utils"
import { useDesktopBridge } from "./bridge-context"
import type {
  DesktopImportTask,
  DesktopImportStageProgressEvent,
  DesktopKnowledgeBase,
} from "./contracts"

type WorkspaceSection = "overview" | "documents" | "answers" | "knowledge" | "review" | "settings"
type DialogMode = "create" | "open"

const navigation: Array<{
  id: WorkspaceSection
  icon: ComponentType<{ className?: string }>
  labelKey: string
}> = [
  { id: "overview", icon: LayoutDashboard, labelKey: "overview" },
  { id: "documents", icon: FileText, labelKey: "documents" },
  { id: "answers", icon: MessageSquare, labelKey: "answers" },
  { id: "knowledge", icon: BookOpen, labelKey: "knowledge" },
  { id: "review", icon: ClipboardCheck, labelKey: "review" },
  { id: "settings", icon: Settings, labelKey: "settings" },
]

let requestSequence = 0

function nextRequestId(): string {
  requestSequence += 1
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `desktop-knowledge-base-${Date.now()}-${requestSequence}`
}

/** The first real Desktop Workbench: one active SQLite knowledge base at a time. */
export default function DesktopKnowledgeBaseWorkspace() {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [knowledgeBase, setKnowledgeBase] = useState<DesktopKnowledgeBase | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [section, setSection] = useState<WorkspaceSection>("overview")
  const [dialogMode, setDialogMode] = useState<DialogMode | null>(null)
  const [path, setPath] = useState("")
  const [name, setName] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [importPath, setImportPath] = useState("")
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState<string | null>(null)
  const [importTasks, setImportTasks] = useState<DesktopImportTask[]>([])
  const [liveImportStage, setLiveImportStage] = useState<
    DesktopImportStageProgressEvent["data"] | null
  >(null)
  const activeKnowledgeBaseRead = useRef(0)
  const activeImportRequest = useRef<string | null>(null)

  const refreshActiveKnowledgeBase = useCallback(async () => {
    const read = activeKnowledgeBaseRead.current + 1
    activeKnowledgeBaseRead.current = read
    try {
      const result = await bridge.activeKnowledgeBase()
      const importTask = result.knowledgeBase === null
        ? []
        : (await bridge.importJobs()).jobs
      if (read !== activeKnowledgeBaseRead.current) return
      setKnowledgeBase(result.knowledgeBase)
      setImportTasks(importTask)
      setLoadError(null)
    } catch (error) {
      if (read !== activeKnowledgeBaseRead.current) return
      setLoadError(error instanceof Error ? error.message : String(error))
    } finally {
      if (read === activeKnowledgeBaseRead.current) setLoading(false)
    }
  }, [bridge])

  useEffect(() => {
    void Promise.resolve().then(refreshActiveKnowledgeBase)
  }, [refreshActiveKnowledgeBase])

  useEffect(() => {
    let unsubscribe: (() => void) | undefined
    let disposed = false
    void bridge
      .subscribe((event) => {
        if (
          event.kind === "import.stage_progress" &&
          event.data.requestId === activeImportRequest.current
        ) {
          setLiveImportStage(event.data)
        }
      })
      .then((remove) => {
        if (disposed) {
          remove()
        } else {
          unsubscribe = remove
        }
      })
      .catch(() => undefined)
    return () => {
      disposed = true
      unsubscribe?.()
    }
  }, [bridge])

  const beginSelection = (mode: DialogMode) => {
    setDialogMode(mode)
    setPath("")
    setName("")
    setFormError(null)
  }

  const closeSelection = (open: boolean) => {
    if (!open) {
      setDialogMode(null)
      setFormError(null)
    }
  }

  const submitSelection = async () => {
    if (dialogMode === null) return
    if (!path.trim()) {
      setFormError(t("desktop.knowledgeBases.pathRequired"))
      return
    }
    setSubmitting(true)
    setFormError(null)
    setImportTasks([])
    setLiveImportStage(null)
    activeImportRequest.current = null
    try {
      if (dialogMode === "create") {
        await bridge.createKnowledgeBase(path.trim(), name.trim() || undefined, nextRequestId())
      } else {
        await bridge.openKnowledgeBase(path.trim(), nextRequestId())
      }
      await refreshActiveKnowledgeBase()
      setSection("overview")
      setDialogMode(null)
    } catch (error) {
      await refreshActiveKnowledgeBase()
      setFormError(error instanceof Error ? error.message : String(error))
    } finally {
      setSubmitting(false)
    }
  }

  const submitTextImport = async () => {
    if (!importPath.trim()) {
      setImportError(t("desktop.knowledgeBases.importPathRequired"))
      return
    }
    setImporting(true)
    setImportError(null)
    setLiveImportStage(null)
    const requestId = nextRequestId()
    activeImportRequest.current = requestId
    try {
      const result = await bridge.importTextDocument(importPath.trim(), requestId)
      if (activeImportRequest.current === requestId) {
        setImportTasks((tasks) => [result, ...tasks.filter((task) => task.job.jobId !== result.job.jobId)])
        setLiveImportStage(null)
      }
      setImportPath("")
    } catch (error) {
      setImportError(error instanceof Error ? error.message : String(error))
      try {
        setImportTasks((await bridge.importJobs()).jobs)
      } catch {
        // The original import error is more useful than a follow-up task lookup failure.
      }
    } finally {
      if (activeImportRequest.current === requestId) activeImportRequest.current = null
      setImporting(false)
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground" data-testid="desktop-workbench">
      <header className="flex min-h-16 items-center justify-between gap-3 border-b border-border/70 bg-background/85 px-4 backdrop-blur md:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary text-primary-foreground shadow-sm">
            <BookOpen className="size-4" />
          </div>
          <div className="min-w-0">
            <p className="font-mono2 text-[10px] font-semibold tracking-[0.2em] text-muted-foreground">
              OPENKB
            </p>
            <button
              type="button"
              onClick={() => beginSelection("open")}
              className="mt-0.5 flex max-w-[min(58vw,34rem)] items-center gap-1.5 rounded-md text-left text-sm font-semibold outline-none transition-colors hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
              aria-haspopup="dialog"
            >
              <span className="truncate">
                {knowledgeBase?.name ?? t("desktop.knowledgeBases.chooseKnowledgeBase")}
              </span>
              <FolderOpen className="size-3.5 shrink-0 text-muted-foreground" />
            </button>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1 rounded-full border border-border/70 bg-muted/45 p-1">
          <ThemeToggle className="text-muted-foreground hover:text-foreground" />
          <LanguageToggle className="text-muted-foreground hover:text-foreground" />
        </div>
      </header>

      <div className="grid min-h-[calc(100vh-4rem)] grid-cols-[13rem_minmax(0,1fr)]">
        <aside className="border-r border-border/70 bg-muted/20 p-3" aria-label={t("desktop.knowledgeBases.navigation")}>
          <nav className="space-y-1">
            {navigation.map(({ id, icon: Icon, labelKey }) => (
              <button
                key={id}
                type="button"
                onClick={() => setSection(id)}
                aria-current={section === id ? "page" : undefined}
                className={cn(
                  "flex h-9 w-full items-center gap-2 rounded-md px-3 text-left text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
                  section === id
                    ? "bg-primary text-primary-foreground shadow-sm"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                )}
              >
                <Icon className="size-4" />
                {t(`desktop.knowledgeBases.navigationItems.${labelKey}`)}
              </button>
            ))}
          </nav>
          <div className="mt-6 border-t border-border/70 pt-4">
            <Button className="w-full justify-start" size="sm" onClick={() => beginSelection("create")}>
              <Plus className="size-4" />
              {t("desktop.knowledgeBases.create")}
            </Button>
            <Button
              className="mt-2 w-full justify-start"
              size="sm"
              variant="outline"
              onClick={() => beginSelection("open")}
            >
              <FolderOpen className="size-4" />
              {t("desktop.knowledgeBases.open")}
            </Button>
          </div>
        </aside>

        <main className="min-w-0 p-5 md:p-8">
          {loading ? (
            <div className="flex min-h-60 items-center justify-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              {t("desktop.knowledgeBases.loading")}
            </div>
          ) : loadError ? (
            <section className="max-w-xl rounded-apple-lg border border-destructive/30 bg-destructive/5 p-5" role="alert">
              <h1 className="font-semibold">{t("desktop.knowledgeBases.loadErrorTitle")}</h1>
              <p className="mt-2 text-sm text-muted-foreground">{loadError}</p>
              <Button
                className="mt-4"
                variant="outline"
                onClick={() => {
                  setLoading(true)
                  void refreshActiveKnowledgeBase()
                }}
              >
                {t("desktop.knowledgeBases.retry")}
              </Button>
            </section>
          ) : knowledgeBase === null ? (
            <EmptyKnowledgeBase onCreate={() => beginSelection("create")} onOpen={() => beginSelection("open")} />
          ) : (
            <ActiveKnowledgeBaseView
              knowledgeBase={knowledgeBase}
              section={section}
              importError={importError}
              importing={importing}
              importPath={importPath}
              importTasks={importTasks}
              liveImportStage={liveImportStage}
              onImportPathChange={setImportPath}
              onSubmitImport={() => void submitTextImport()}
            />
          )}
        </main>
      </div>

      <Dialog open={dialogMode !== null} onOpenChange={closeSelection}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {dialogMode === "create"
                ? t("desktop.knowledgeBases.createTitle")
                : t("desktop.knowledgeBases.openTitle")}
            </DialogTitle>
            <DialogDescription>
              {dialogMode === "create"
                ? t("desktop.knowledgeBases.createDescription")
                : t("desktop.knowledgeBases.openDescription")}
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              void submitSelection()
            }}
          >
            <div>
              <label className="text-sm font-medium" htmlFor="desktop-kb-path">
                {t("desktop.knowledgeBases.pathLabel")}
              </label>
              <input
                id="desktop-kb-path"
                autoFocus
                value={path}
                onChange={(event) => setPath(event.target.value)}
                placeholder={t("desktop.knowledgeBases.pathPlaceholder")}
                className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            {dialogMode === "create" ? (
              <div>
                <label className="text-sm font-medium" htmlFor="desktop-kb-name">
                  {t("desktop.knowledgeBases.nameLabel")}
                </label>
                <input
                  id="desktop-kb-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder={t("desktop.knowledgeBases.namePlaceholder")}
                  className="mt-2 h-10 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </div>
            ) : null}
            {formError ? <p className="text-sm text-destructive" role="alert">{formError}</p> : null}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => closeSelection(false)}>
                {t("actions.cancel")}
              </Button>
              <Button type="submit" disabled={submitting}>
                {submitting ? <Loader2 className="size-4 animate-spin" /> : null}
                {dialogMode === "create"
                  ? t("desktop.knowledgeBases.create")
                  : t("desktop.knowledgeBases.open")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function EmptyKnowledgeBase({ onCreate, onOpen }: { onCreate: () => void; onOpen: () => void }) {
  const { t } = useTranslation("common")
  return (
    <section className="mx-auto flex min-h-80 max-w-2xl flex-col justify-center rounded-apple-lg border border-border/70 bg-muted/20 p-8 shadow-sm">
      <span className="grid size-12 place-items-center rounded-2xl bg-primary/10 text-primary">
        <BookOpen className="size-6" />
      </span>
      <h1 className="mt-5 text-2xl font-semibold tracking-tight">{t("desktop.knowledgeBases.emptyTitle")}</h1>
      <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">
        {t("desktop.knowledgeBases.emptyDescription")}
      </p>
      <div className="mt-6 flex flex-wrap gap-3">
        <Button onClick={onCreate}>
          <Plus className="size-4" />
          {t("desktop.knowledgeBases.create")}
        </Button>
        <Button variant="outline" onClick={onOpen}>
          <FolderOpen className="size-4" />
          {t("desktop.knowledgeBases.open")}
        </Button>
      </div>
    </section>
  )
}

function ActiveKnowledgeBaseView({
  knowledgeBase,
  section,
  importError,
  importing,
  importPath,
  importTasks,
  liveImportStage,
  onImportPathChange,
  onSubmitImport,
}: {
  knowledgeBase: DesktopKnowledgeBase
  section: WorkspaceSection
  importError: string | null
  importing: boolean
  importPath: string
  importTasks: DesktopImportTask[]
  liveImportStage: DesktopImportStageProgressEvent["data"] | null
  onImportPathChange: (value: string) => void
  onSubmitImport: () => void
}) {
  const { t } = useTranslation("common")
  const sectionTitle = t(`desktop.knowledgeBases.navigationItems.${section}`)
  return (
    <section className="mx-auto max-w-4xl">
      <p className="font-mono2 text-xs font-semibold tracking-[0.18em] text-primary">{sectionTitle}</p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight">{knowledgeBase.name}</h1>
      <p className="mt-2 break-all text-sm text-muted-foreground">{knowledgeBase.kbDir}</p>
      <div className="mt-7 grid gap-4 sm:grid-cols-3">
        <StatusCard label={t("desktop.knowledgeBases.schemaVersion")} value={`v${knowledgeBase.schemaVersion}`} />
        <StatusCard label={t("desktop.knowledgeBases.checkpoint")} value={knowledgeBase.lastCheckpointAt ?? t("desktop.knowledgeBases.notYet")} />
        <StatusCard label={t("desktop.knowledgeBases.runtime")} value={t("desktop.knowledgeBases.active")} />
      </div>
      <div className="mt-8 rounded-apple-lg border border-border/70 bg-muted/20 p-6">
        <div className="flex items-start gap-3">
          <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
          <div>
            <h2 className="font-semibold">{t("desktop.knowledgeBases.readyTitle", { section: sectionTitle })}</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              {t("desktop.knowledgeBases.readyDescription")}
            </p>
          </div>
        </div>
      </div>
      {section === "documents" ? (
        <DocumentImportPanel
          error={importError}
          importing={importing}
          path={importPath}
          tasks={importTasks}
          liveStage={liveImportStage}
          onPathChange={onImportPathChange}
          onSubmit={onSubmitImport}
        />
      ) : null}
    </section>
  )
}

function DocumentImportPanel({
  error,
  importing,
  path,
  tasks,
  liveStage,
  onPathChange,
  onSubmit,
}: {
  error: string | null
  importing: boolean
  path: string
  tasks: DesktopImportTask[]
  liveStage: DesktopImportStageProgressEvent["data"] | null
  onPathChange: (value: string) => void
  onSubmit: () => void
}) {
  const { t } = useTranslation("common")
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
      <form
        className="mt-5 flex flex-col gap-3 sm:flex-row"
        onSubmit={(event) => {
          event.preventDefault()
          onSubmit()
        }}
      >
        <label className="sr-only" htmlFor="desktop-import-txt-path">
          {t("desktop.knowledgeBases.importPathLabel")}
        </label>
        <input
          id="desktop-import-txt-path"
          value={path}
          onChange={(event) => onPathChange(event.target.value)}
          placeholder={t("desktop.knowledgeBases.importPathPlaceholder")}
          className="h-10 min-w-0 flex-1 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Button type="submit" disabled={importing}>
          {importing ? <Loader2 className="size-4 animate-spin" /> : <Upload className="size-4" />}
          {t("desktop.knowledgeBases.importAction")}
        </Button>
      </form>
      {error ? <p className="mt-3 text-sm text-destructive" role="alert">{error}</p> : null}
      {liveStage ? <ImportTaskCard className="mt-5" stage={liveStage} /> : null}
      {tasks.map((task) => (
        <ImportTaskCard key={task.job.jobId} className="mt-5" task={task} />
      ))}
    </section>
  )
}

function ImportTaskCard({
  className,
  stage: liveStage,
  task,
}: {
  className?: string
  stage?: DesktopImportStageProgressEvent["data"]
  task?: DesktopImportTask
}) {
  const { t } = useTranslation("common")
  const stage = liveStage ?? task?.stages.find((item) => item.status === "failed") ?? task?.stages.find(
    (item) => item.status === "running",
  ) ?? task?.stages.at(-1)
  if (!stage) return null
  const jobStatus = task?.job.status ?? stage.status
  const jobProgress = task?.job.progress ?? stage.progress
  return (
    <div className={cn("rounded-xl border border-border/70 bg-muted/30 p-4", className)}>
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="font-medium">{t("desktop.knowledgeBases.taskCenter")}</span>
        <span className="text-muted-foreground">{jobProgress}%</span>
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
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${jobProgress}%` }} />
      </div>
      {task?.document ? (
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
        </div>
      ) : null}
    </div>
  )
}

function StatusCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-border/70 bg-background p-4 shadow-sm">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="mt-2 break-all text-sm font-semibold text-foreground">{value}</p>
    </div>
  )
}
