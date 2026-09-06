import {
  FolderOpen,
  Loader2,
} from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useDesktopBridge } from "@/desktop/bridge/context"
import type { DesktopImportBatchSummary } from "@/desktop/features/documents/DesktopDocumentImportPanel"
import { DesktopGlobalSearchDialog } from "@/desktop/features/search/DesktopGlobalSearchDialog"
import { ActiveKnowledgeBaseView, EmptyKnowledgeBase } from "@/desktop/app/DesktopKnowledgeBaseViews"
import DesktopLocalSettingsPanel from "@/desktop/features/settings/DesktopLocalSettingsPanel"
import { DesktopTaskDrawer } from "@/desktop/features/tasks/DesktopTaskDrawer"
import { FailedDocumentsDialog } from "@/desktop/features/documents/FailedDocumentsDialog"
import { DesktopRawDocumentDialog } from "@/desktop/features/documents/DesktopRawDocumentDialog"
import {
  DesktopWorkbenchShell,
  type WorkspaceSection,
} from "@/desktop/app/DesktopWorkbenchShell"
import { DesktopBridgeError } from "@/desktop/bridge/contracts"
import { runDocumentImportBatch } from "@/desktop/features/documents/desktop-import-batch"
import { createLatestRefresh, type LatestRefresh } from "@/desktop/shared/latest-refresh"
import { nextDesktopRequestId } from "@/desktop/shared/request-id"
import { useDeferredImportSources } from "@/desktop/app/useDeferredImportSources"
import { useDesktopRuntimeEvents } from "@/desktop/app/useDesktopRuntimeEvents"
import { useKnowledgeReanalysis } from "@/desktop/features/tasks/useKnowledgeReanalysis"
import type {
  DesktopImportTask,
  DesktopGlobalSearchResult,
  DesktopImportSourceInspection,
  DesktopImportSourcePicker,
  DesktopKnowledgeBase,
  DesktopRawDocument,
  DesktopRecoveryOverride,
} from "@/desktop/bridge/contracts"

type DialogMode = "create" | "open"
type ImportTaskAction = "pause" | "resume" | "cancel"

const LAST_SECTION_PREFIX = "openkb.desktop.last-section."

function storedSection(kbDir: string): WorkspaceSection {
  const value = window.localStorage.getItem(`${LAST_SECTION_PREFIX}${kbDir}`)
  return ["overview", "documents", "conversations", "knowledge", "settings"].includes(value ?? "")
    ? value as WorkspaceSection
    : "overview"
}

function isImportControlError(error: unknown): boolean {
  return error instanceof DesktopBridgeError
    && (error.code === "import_paused"
      || error.code === "import_cancelled"
      || error.code === "document_quarantined")
}

function mergeImportSources(existing: string[], added: string[]): string[] {
  const values = new Map<string, string>()
  for (const sourcePath of [...existing, ...added]) {
    const trimmed = sourcePath.trim()
    if (trimmed) values.set(importSourceKey(trimmed), trimmed)
  }
  return [...values.values()].sort((left, right) => left.localeCompare(right))
}

function importSourceKey(sourcePath: string): string {
  return sourcePath.trim().toLowerCase()
}

function isKnowledgeBaseDirectoryName(value: string): boolean {
  const candidate = value.trim()
  return Boolean(candidate) && candidate !== "." && candidate !== ".." && !/[\\/]/.test(candidate)
}

function withoutExcludedImportSources(
  inspection: DesktopImportSourceInspection,
  excludedSourcePaths: string[],
): DesktopImportSourceInspection {
  const excluded = new Set(excludedSourcePaths.map(importSourceKey))
  return {
    ...inspection,
    supported: inspection.supported.filter((source) => !excluded.has(importSourceKey(source.path))),
    unsupported: inspection.unsupported.filter((source) => !excluded.has(importSourceKey(source.path))),
  }
}

/** The first real Desktop Workbench: one active SQLite knowledge base at a time. */
export default function DesktopKnowledgeBaseWorkspace({ engineReady = true }: { engineReady?: boolean }) {
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
  const [importSources, setImportSources] = useState<string[]>([])
  const [excludedImportSources, setExcludedImportSources] = useState<string[]>([])
  const [importInspection, setImportInspection] = useState<DesktopImportSourceInspection | null>(null)
  const [inspectingImportSources, setInspectingImportSources] = useState(false)
  const [importDropActive, setImportDropActive] = useState(false)
  const [importBatchSummary, setImportBatchSummary] = useState<DesktopImportBatchSummary | null>(null)
  const [importTasks, setImportTasks] = useState<DesktopImportTask[]>([])
  const [rawDocument, setRawDocument] = useState<DesktopRawDocument | null>(null)
  const [rawDocumentFocus, setRawDocumentFocus] = useState<Record<string, unknown> | null>(null)
  const [loadingRawDocument, setLoadingRawDocument] = useState(false)
  const [failedDocumentsOpen, setFailedDocumentsOpen] = useState(false)
  const [taskDrawerOpen, setTaskDrawerOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const [requestedConversationId, setRequestedConversationId] = useState<string | null>(null)
  const [requestedConversationMessageId, setRequestedConversationMessageId] = useState<string | null>(null)
  const [requestedDocumentId, setRequestedDocumentId] = useState<string | null>(null)
  const [requestedKnowledgePageId, setRequestedKnowledgePageId] = useState<string | null>(null)
  const [knowledgeInitialTab, setKnowledgeInitialTab] = useState<"pages" | "review">("pages")
  const [navigationRequestSequence, setNavigationRequestSequence] = useState(0)
  const [reviewCount, setReviewCount] = useState(0)
  const [reviewRefreshKey, setReviewRefreshKey] = useState(0)
  const [controllingJobId, setControllingJobId] = useState<string | null>(null)
  const activeKnowledgeBaseRead = useRef(0)
  const activeKnowledgeBasePath = useRef<string | null>(null)
  const initialActiveKnowledgeBaseRead = useRef(true)
  const trayTipShown = useRef(false)
  const importInspectionRead = useRef(0)
  const addImportSourcesRef = useRef<(paths: string[]) => void>(() => undefined)
  const importTaskRefresh = useRef<LatestRefresh | null>(null)
  const activeKnowledgeBaseDirectory = knowledgeBase?.kbDir ?? null
  const knowledgeReanalysis = useKnowledgeReanalysis({ bridge, kbDir: knowledgeBase?.kbDir ?? null, engineReady })

  const refreshActiveKnowledgeBase = useCallback(async () => {
    const read = activeKnowledgeBaseRead.current + 1
    activeKnowledgeBaseRead.current = read
    try {
      const result = await bridge.activeKnowledgeBase()
      const importTask = result.knowledgeBase === null
        ? []
        : (await bridge.importJobs()).jobs
      if (read !== activeKnowledgeBaseRead.current) return
      const shouldAnnounceRestoration = initialActiveKnowledgeBaseRead.current
      initialActiveKnowledgeBaseRead.current = false
      if (activeKnowledgeBasePath.current !== result.knowledgeBase?.kbDir) {
        activeKnowledgeBasePath.current = result.knowledgeBase?.kbDir ?? null
        setSection(result.knowledgeBase ? storedSection(result.knowledgeBase.kbDir) : "overview")
        setRequestedConversationId(null)
        setRequestedConversationMessageId(null)
        setRequestedDocumentId(null)
        setRequestedKnowledgePageId(null)
        setKnowledgeInitialTab("pages")
        setReviewCount(0)
        setSearchOpen(false)
      }
      setKnowledgeBase(result.knowledgeBase)
      setImportTasks(importTask)
      setLoadError(null)
      if (shouldAnnounceRestoration && result.knowledgeBase !== null) {
        toast.success(t("desktop.knowledgeBases.runtimeRestored", { name: result.knowledgeBase.name }))
      }
    } catch (error) {
      if (read !== activeKnowledgeBaseRead.current) return
      setLoadError(error instanceof Error ? error.message : String(error))
    } finally {
      if (read === activeKnowledgeBaseRead.current) setLoading(false)
    }
  }, [bridge, t])

  const handleRuntimeNotice = useCallback((notice: "previousKnowledgeBaseUnavailable" | "trayRestored" | "engineRestarted") => {
    if (notice === "previousKnowledgeBaseUnavailable") {
      toast.error(t("desktop.knowledgeBases.previousKnowledgeBaseUnavailable"))
      return
    }
    if (notice === "trayRestored") {
      if (trayTipShown.current) return
      trayTipShown.current = true
      toast.success(t("desktop.knowledgeBases.trayRestored"))
      return
    }
    toast.info(t("desktop.knowledgeBases.engineRestarted"))
  }, [t])

  const openTaskDrawer = useCallback(() => setTaskDrawerOpen(true), [])

  const changeSection = useCallback((next: WorkspaceSection) => {
    setSection(next)
    if (knowledgeBase) {
      window.localStorage.setItem(`${LAST_SECTION_PREFIX}${knowledgeBase.kbDir}`, next)
    }
  }, [knowledgeBase])

  const openSearch = useCallback(() => {
    if (knowledgeBase === null) {
      toast.info(t("desktop.globalSearch.openKnowledgeBaseFirst"))
      return
    }
    setSearchOpen(true)
  }, [knowledgeBase, t])

  const selectSearchResult = (result: DesktopGlobalSearchResult) => {
    setNavigationRequestSequence((current) => current + 1)
    if (result.kind === "document") {
      setRequestedDocumentId(result.documentId)
      changeSection("documents")
      if (!result.documentId && result.status === "failed") setFailedDocumentsOpen(true)
      return
    }
    if (result.kind === "knowledge_page" && result.pageId) {
      setRequestedKnowledgePageId(result.pageId)
      setKnowledgeInitialTab("pages")
      changeSection("knowledge")
      return
    }
    if (result.conversationId) {
      setRequestedConversationId(result.conversationId)
      setRequestedConversationMessageId(result.messageId)
      changeSection("conversations")
    }
  }

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault()
        openSearch()
      }
    }
    window.addEventListener("keydown", handleShortcut)
    return () => window.removeEventListener("keydown", handleShortcut)
  }, [openSearch])

  useEffect(() => {
    void Promise.resolve().then(refreshActiveKnowledgeBase)
  }, [refreshActiveKnowledgeBase])

  useEffect(() => {
    let disposed = false
    if (knowledgeBase === null || !engineReady) {
      return
    }
    void Promise.all([
      bridge.knowledgeReconciliationConflicts(),
      bridge.documentVersionCandidates(),
      bridge.missingSourceCandidates(),
    ]).then(([conflicts, versions, missingSources]) => {
      if (!disposed) {
        setReviewCount(
          conflicts.conflicts.length
          + versions.candidates.filter((item) => item.status === "pending").length
          + missingSources.candidates.length,
        )
      }
    }).catch(() => undefined)
    return () => { disposed = true }
  }, [bridge, engineReady, importTasks, knowledgeBase, reviewRefreshKey])

  useEffect(() => {
    let unsubscribe: (() => void) | undefined
    let disposed = false
    const refresh = createLatestRefresh({
      load: () => bridge.importJobs(),
      commit: ({ jobs }) => setImportTasks(jobs),
    })
    importTaskRefresh.current = refresh
    void bridge
      .subscribe((event) => {
        if (event.kind === "import.stage_progress" && activeKnowledgeBaseDirectory !== null) {
          refresh.request()
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
      refresh.dispose()
      if (importTaskRefresh.current === refresh) importTaskRefresh.current = null
      unsubscribe?.()
    }
  }, [activeKnowledgeBaseDirectory, bridge])

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

  const chooseKnowledgeBaseDirectory = async () => {
    try {
      const selected = await bridge.chooseKnowledgeBaseDirectory()
      if (selected) {
        setPath(selected)
        setFormError(null)
      }
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error))
    }
  }

  const submitSelection = async () => {
    if (dialogMode === null) return
    if (!path.trim()) {
      setFormError(t("desktop.knowledgeBases.directoryRequired"))
      return
    }
    if (dialogMode === "create" && !isKnowledgeBaseDirectoryName(name)) {
      setFormError(t("desktop.knowledgeBases.nameRequired"))
      return
    }
    setSubmitting(true)
    setFormError(null)
    setImportTasks([])
    setRawDocument(null)
    setRawDocumentFocus(null)
    setLoadingRawDocument(false)
    setImportSources([])
    setExcludedImportSources([])
    importInspectionRead.current += 1
    setImportInspection(null)
    setInspectingImportSources(false)
    setImportBatchSummary(null)
    try {
      let activatedKbDir = path.trim()
      if (dialogMode === "create") {
        const { join } = await import("@tauri-apps/api/path")
        activatedKbDir = await join(path.trim(), name.trim())
        await bridge.createKnowledgeBase(activatedKbDir, name.trim(), nextDesktopRequestId("knowledge-base"))
      } else {
        await bridge.openKnowledgeBase(activatedKbDir, nextDesktopRequestId("knowledge-base"))
      }
      await refreshActiveKnowledgeBase()
      const nextSection: WorkspaceSection = dialogMode === "create"
        ? "documents"
        : storedSection(activatedKbDir)
      setSection(nextSection)
      window.localStorage.setItem(`${LAST_SECTION_PREFIX}${activatedKbDir}`, nextSection)
      setDialogMode(null)
    } catch (error) {
      await refreshActiveKnowledgeBase()
      setFormError(error instanceof Error ? error.message : String(error))
    } finally {
      setSubmitting(false)
    }
  }

  const inspectImportSources = useCallback(async (
    sourcePaths: string[],
    excludedSourcePaths = excludedImportSources,
  ) => {
    const read = importInspectionRead.current + 1
    importInspectionRead.current = read
    if (!sourcePaths.length) {
      setImportInspection(null)
      setInspectingImportSources(false)
      return
    }
    setInspectingImportSources(true)
    setImportError(null)
    try {
      const inspection = await bridge.inspectImportSources(sourcePaths, nextDesktopRequestId("knowledge-base"))
      if (read === importInspectionRead.current) {
        setImportInspection(withoutExcludedImportSources(inspection, excludedSourcePaths))
      }
    } catch (error) {
      if (read === importInspectionRead.current) {
        setImportInspection(null)
        setImportError(error instanceof Error ? error.message : String(error))
      }
    } finally {
      if (read === importInspectionRead.current) setInspectingImportSources(false)
    }
  }, [bridge, excludedImportSources])
  const selectImportSources = useCallback((sourcePaths: string[]) => {
    const nextSources = mergeImportSources(importSources, sourcePaths)
    if (!nextSources.length) return
    const selectedSourceKeys = new Set(sourcePaths.map(importSourceKey))
    const nextExcludedSources = excludedImportSources.filter(
      (sourcePath) => !selectedSourceKeys.has(importSourceKey(sourcePath)),
    )
    setImportSources(nextSources)
    setExcludedImportSources(nextExcludedSources)
    setImportBatchSummary(null)
    void inspectImportSources(nextSources, nextExcludedSources)
  }, [excludedImportSources, importSources, inspectImportSources])
  const addImportSources = useDeferredImportSources(importing, selectImportSources)

  useEffect(() => {
    addImportSourcesRef.current = addImportSources
  }, [addImportSources])
  useDesktopRuntimeEvents({
    bridge,
    importSourcesRef: addImportSourcesRef,
    refreshActiveKnowledgeBase,
    setLoading,
    setLoadError,
    setSection: changeSection,
    onOpenTasks: openTaskDrawer,
    onRuntimeNotice: handleRuntimeNotice,
  })

  useEffect(() => {
    let unsubscribe: (() => void) | undefined
    let disposed = false
    void bridge
      .subscribeImportDrops((event) => {
        if (event.type === "enter" || event.type === "over") setImportDropActive(true)
        if (event.type === "leave" || event.type === "drop") setImportDropActive(false)
        if (event.type === "drop" && event.paths.length) addImportSourcesRef.current(event.paths)
      })
      .then((remove) => {
        if (disposed) remove()
        else unsubscribe = remove
      })
      .catch(() => undefined)
    return () => {
      disposed = true
      unsubscribe?.()
    }
  }, [bridge])

  const addManualImportSource = () => {
    if (!importPath.trim()) {
      setImportError(t("desktop.knowledgeBases.importPathRequired"))
      return
    }
    addImportSources([importPath.trim()])
    setImportPath("")
  }

  const chooseImportSources = async (picker: DesktopImportSourcePicker) => {
    try {
      addImportSources(await bridge.chooseImportSources(picker))
    } catch (error) {
      setImportError(error instanceof Error ? error.message : String(error))
    }
  }

  const removeImportSource = (sourcePath: string) => {
    const nextExcludedSources = mergeImportSources(excludedImportSources, [sourcePath])
    setExcludedImportSources(nextExcludedSources)
    importInspectionRead.current += 1
    setImportInspection((inspection) => (
      inspection === null ? null : withoutExcludedImportSources(inspection, nextExcludedSources)
    ))
  }

  const submitImportBatch = async () => {
    if (importInspection === null || !importInspection.supported.length) {
      setImportError(t("desktop.knowledgeBases.noImportableSources"))
      return
    }
    setImporting(true)
    setImportError(null)
    const batchTotal = importInspection.supported.length
    setImportBatchSummary({ total: batchTotal, completed: 0, failures: [], running: true })
    let completed = 0
    const failures: Array<{ name: string; reason: string }> = []
    await runDocumentImportBatch(importInspection.supported, async (source) => {
      const requestId = nextDesktopRequestId("knowledge-base")
      try {
        const result = await bridge.importTextDocument(source.path, requestId)
        completed += 1
        setImportTasks((tasks) => [
          result,
          ...tasks.filter((task) => task.job.jobId !== result.job.jobId),
        ])
      } catch (error) {
        failures.push({
          name: source.name,
          reason: error instanceof Error ? error.message : String(error),
        })
      }
      setImportBatchSummary({
        total: batchTotal,
        completed,
        failures: [...failures],
        running: true,
      })
    })
    importTaskRefresh.current?.request()
    setImportBatchSummary({ total: batchTotal, completed, failures, running: false })
    setImportSources([])
    setExcludedImportSources([])
    setImportInspection(null)
    setImporting(false)
  }

  const openRawDocument = async (
    documentId: string,
    locator: Record<string, unknown> | null = null,
  ) => {
    setImportError(null)
    setRawDocumentFocus(locator)
    setLoadingRawDocument(true)
    try {
      setRawDocument(await bridge.readRawDocument(
        documentId,
        nextDesktopRequestId("knowledge-base"),
        0,
        locator ?? undefined,
      ))
    } catch (error) {
      setImportError(error instanceof Error ? error.message : String(error))
      await refreshActiveKnowledgeBase()
    } finally {
      setLoadingRawDocument(false)
    }
  }

  const loadMoreRawDocument = async () => {
    const current = rawDocument
    if (!current?.hasMore || loadingRawDocument) return
    setImportError(null)
    setLoadingRawDocument(true)
    try {
      const next = await bridge.readRawDocument(
        current.documentId,
        nextDesktopRequestId("knowledge-base"),
        current.page + 1,
      )
      setRawDocument((displayed) => (
        displayed?.documentId === current.documentId && displayed.page === current.page
          ? { ...next, content: displayed.content + next.content }
          : displayed
      ))
    } catch (error) {
      setImportError(error instanceof Error ? error.message : String(error))
      await refreshActiveKnowledgeBase()
    } finally {
      setLoadingRawDocument(false)
    }
  }

  const controlImportJob = async (jobId: string, action: ImportTaskAction) => {
    setControllingJobId(jobId)
    setImportError(null)
    try {
      if (action === "pause") {
        await bridge.pauseImportJob(jobId)
      } else if (action === "cancel") {
        await bridge.cancelImportJob(jobId)
      } else {
        await bridge.resumeImportJob(jobId, nextDesktopRequestId("knowledge-base"))
      }
    } catch (error) {
      if (!isImportControlError(error)) {
        setImportError(error instanceof Error ? error.message : String(error))
      }
    } finally {
      await refreshActiveKnowledgeBase()
      setControllingJobId(null)
    }
  }

  const recoverImportJob = async (jobId: string, recoveryOverride: DesktopRecoveryOverride) => {
    setControllingJobId(jobId)
    setImportError(null)
    try {
      await bridge.recoverImportJob(jobId, recoveryOverride, nextDesktopRequestId("knowledge-base"))
    } catch (error) {
      if (!isImportControlError(error)) {
        setImportError(error instanceof Error ? error.message : String(error))
      }
    } finally {
      await refreshActiveKnowledgeBase()
      setControllingJobId(null)
    }
  }

  const activeTaskCount = importTasks.filter((task) => ["pending", "running", "paused", "recoverable"].includes(task.job.status)).length + knowledgeReanalysis.activeJobCount

  return (
    <>
      <DesktopWorkbenchShell
        activeSection={section}
        knowledgeBaseName={knowledgeBase?.name ?? null}
        engineReady={engineReady}
        activeTaskCount={activeTaskCount}
        reviewCount={reviewCount}
        onSectionChange={(next) => {
          if (next === "knowledge") setKnowledgeInitialTab("pages")
          changeSection(next)
        }}
        onOpenReview={() => {
          setKnowledgeInitialTab("review")
          setNavigationRequestSequence((current) => current + 1)
          changeSection("knowledge")
        }}
        onOpenKnowledgeBase={() => beginSelection("open")}
        onCreateKnowledgeBase={() => beginSelection("create")}
        onOpenSearch={openSearch}
        onOpenTasks={() => setTaskDrawerOpen(true)}
      >
        <div className="min-h-[calc(100vh-3.5rem)] p-4 md:p-6">
          {section === "settings" && knowledgeBase === null ? (
            <DesktopLocalSettingsPanel />
          ) : loading ? (
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
              importSources={importSources}
              importInspection={importInspection}
              inspectingImportSources={inspectingImportSources}
              importDropActive={importDropActive}
              importBatchSummary={importBatchSummary}
              importTasks={importTasks}
              controllingJobId={controllingJobId}
              knowledgeReanalysis={knowledgeReanalysis}
              requestedConversationId={requestedConversationId}
              requestedConversationMessageId={requestedConversationMessageId}
              requestedDocumentId={requestedDocumentId}
              requestedKnowledgePageId={requestedKnowledgePageId}
              knowledgeInitialTab={knowledgeInitialTab}
              navigationRequestSequence={navigationRequestSequence}
              onImportPathChange={setImportPath}
              onAddImportPath={addManualImportSource}
              onChooseImportSources={(picker) => void chooseImportSources(picker)}
              onRemoveImportSource={removeImportSource}
              onSubmitImport={() => void submitImportBatch()}
              onControlImportJob={(jobId, action) => void controlImportJob(jobId, action)}
              onOpenRawDocument={(documentId, locator) => void openRawDocument(documentId, locator)}
              onNavigate={changeSection}
              onOpenReview={() => {
                setKnowledgeInitialTab("review")
                setNavigationRequestSequence((current) => current + 1)
                changeSection("knowledge")
              }}
              onOpenFailedDocuments={() => setFailedDocumentsOpen(true)}
              onReviewChanged={() => setReviewRefreshKey((value) => value + 1)}
            />
          )}
        </div>
      </DesktopWorkbenchShell>

      <DesktopGlobalSearchDialog
        key={knowledgeBase?.kbDir ?? "no-knowledge-base"}
        open={searchOpen}
        onOpenChange={setSearchOpen}
        onSelect={selectSearchResult}
      />

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
                {dialogMode === "create"
                  ? t("desktop.knowledgeBases.parentDirectoryLabel")
                  : t("desktop.knowledgeBases.pathLabel")}
              </label>
              <div className="mt-2 flex gap-2">
                <input
                  id="desktop-kb-path"
                  readOnly
                  value={path}
                  placeholder={t("desktop.knowledgeBases.pathPlaceholder")}
                  className="h-10 min-w-0 flex-1 rounded-md border border-input bg-muted/20 px-3 text-sm text-muted-foreground outline-none"
                />
                <Button type="button" variant="outline" onClick={() => void chooseKnowledgeBaseDirectory()}>
                  <FolderOpen className="size-4" />
                  {t("desktop.knowledgeBases.chooseDirectory")}
                </Button>
              </div>
            </div>
            {dialogMode === "create" ? (
              <div>
                <label className="text-sm font-medium" htmlFor="desktop-kb-name">
                  {t("desktop.knowledgeBases.nameLabel")}
                </label>
                <input
                  id="desktop-kb-name"
                  autoFocus
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
      <FailedDocumentsDialog
        open={failedDocumentsOpen}
        tasks={importTasks}
        recoveringJobId={controllingJobId}
        onOpenChange={setFailedDocumentsOpen}
        onRecover={(jobId, override) => void recoverImportJob(jobId, override)}
      />
      <DesktopTaskDrawer
        open={taskDrawerOpen}
        batchSummary={importBatchSummary}
        tasks={importTasks}
        controllingJobId={controllingJobId}
        knowledgeReanalysis={knowledgeReanalysis}
        bridge={bridge}
        kbDir={knowledgeBase?.kbDir ?? null}
        engineReady={engineReady}
        onOpenChange={setTaskDrawerOpen}
        onControl={(jobId, action) => void controlImportJob(jobId, action)}
      />
      <DesktopRawDocumentDialog
        document={rawDocument}
        focusLocator={rawDocumentFocus}
        loadingMore={loadingRawDocument}
        onLoadMore={() => void loadMoreRawDocument()}
        onOpenChange={(open) => {
          if (!open) {
            setRawDocument(null)
            setRawDocumentFocus(null)
            setLoadingRawDocument(false)
          }
        }}
      />
    </>
  )
}
