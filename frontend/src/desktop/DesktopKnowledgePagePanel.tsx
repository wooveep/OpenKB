import { BookMarked, FilePlus2, Link2, Loader2, Save, Search, ShieldCheck, Upload } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import MarkdownView from "@/components/MarkdownView"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { useDesktopBridge } from "./bridge-context"
import { DesktopKnowledgeLifecycleControls } from "./DesktopKnowledgeLifecycleControls"
import { nextDesktopRequestId } from "./request-id"
import type {
  DesktopKnowledgePage,
  DesktopKnowledgePageKind,
  DesktopKnowledgeLifecycleState,
  DesktopKnowledgePagePublicationState,
  DesktopKnowledgeProvenanceState,
  DesktopKnowledgePageSummary,
  DesktopKnowledgePublicationDiagnostic,
  DesktopKnowledgeVerificationStatus,
  DesktopKnowledgeSourceCandidate,
  DesktopKnowledgeSourceMapEntry,
} from "./contracts"

type KnowledgePageEditor = {
  pageId: string | undefined
  kind: DesktopKnowledgePageKind
  title: string
  contentMarkdown: string
  publicationState: DesktopKnowledgePagePublicationState
  provenanceState: DesktopKnowledgeProvenanceState
  verification: DesktopKnowledgeVerificationStatus
  publishedRevisionNumber: number | null
  lifecycleState: DesktopKnowledgeLifecycleState
  staleAfter: string | null
  isStale: boolean
  sourceMap: DesktopKnowledgeSourceMapEntry[]
  publicationDiagnostics: DesktopKnowledgePublicationDiagnostic[]
}

type DraftSaveState = "published" | "unsaved" | "saving" | "saved"

function newEditor(kind: DesktopKnowledgePageKind): KnowledgePageEditor {
  return {
    pageId: undefined,
    kind,
    title: "",
    contentMarkdown: "",
    publicationState: "draft",
    provenanceState: "structural",
    verification: {
      state: "unverified",
      canVerify: false,
      reason: "publish_required",
      actor: null,
      verifiedAt: null,
      revisionId: null,
    },
    publishedRevisionNumber: null,
    lifecycleState: "draft",
    staleAfter: null,
    isStale: false,
    sourceMap: [],
    publicationDiagnostics: [],
  }
}

/** Edit an autosaved Working Draft and publish it only through an explicit action. */
export function DesktopKnowledgePagePanel({ requestedPageId }: { requestedPageId?: string | null }) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [pages, setPages] = useState<DesktopKnowledgePageSummary[]>([])
  const [editor, setEditor] = useState<KnowledgePageEditor>(() => newEditor("concept"))
  const [loading, setLoading] = useState(true)
  const [loadingPage, setLoadingPage] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [mutatingLifecycle, setMutatingLifecycle] = useState(false)
  const [saveState, setSaveState] = useState<DraftSaveState>("unsaved")
  const [error, setError] = useState<string | null>(null)
  const [editTick, setEditTick] = useState(0)
  const [selectedClaim, setSelectedClaim] = useState("")
  const [sourceQuery, setSourceQuery] = useState("")
  const [sourceResults, setSourceResults] = useState<DesktopKnowledgeSourceCandidate[]>([])
  const [searchingSources, setSearchingSources] = useState(false)
  const [bindingSource, setBindingSource] = useState(false)
  const pageRead = useRef(0)
  const editorRef = useRef(editor)
  const pageIdRef = useRef<string | undefined>(undefined)
  const dirtyRef = useRef(false)
  const editVersionRef = useRef(0)
  const saveChainRef = useRef<Promise<void>>(Promise.resolve())
  const autosaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  const refreshPages = useCallback(async () => {
    const result = await bridge.knowledgePages()
    setPages(result.pages)
    return result
  }, [bridge])

  const updatePageSummary = useCallback((page: DesktopKnowledgePage) => {
    const summary: DesktopKnowledgePageSummary = {
      pageId: page.pageId,
      kind: page.kind,
      title: page.title,
      publicationState: page.publicationState,
      publishedRevisionNumber: page.publishedRevisionNumber,
      updatedAt: page.updatedAt,
      lifecycleState: page.lifecycleState,
      staleAfter: page.staleAfter,
      isStale: page.isStale,
    }
    setPages((current) => [summary, ...current.filter((item) => item.pageId !== page.pageId)])
  }, [])

  const applyServerPage = useCallback((page: DesktopKnowledgePage) => {
    const next = editorFromPage(page)
    editorRef.current = next
    pageIdRef.current = page.pageId
    dirtyRef.current = false
    setEditor(next)
    setSaveState(page.workingDraft ? "saved" : "published")
  }, [])

  const queueDraftSave = useCallback((snapshot: KnowledgePageEditor, editVersion: number) => {
    if (mountedRef.current) setSaveState("saving")
    const operation = saveChainRef.current.then(() => bridge.saveKnowledgePage(
      snapshot.pageId ?? pageIdRef.current,
      snapshot.kind,
      snapshot.title,
      snapshot.contentMarkdown,
      nextDesktopRequestId("knowledge-page-draft"),
    ))
    void operation.then(
      (page) => {
        pageIdRef.current = page.pageId
        if (!mountedRef.current) return
        updatePageSummary(page)
        if (editVersion === editVersionRef.current) {
          applyServerPage(page)
          setError(null)
        } else {
          const current = { ...editorRef.current, pageId: page.pageId }
          editorRef.current = current
          setEditor(current)
          setSaveState("unsaved")
        }
      },
      (reason) => {
        if (!mountedRef.current) return
        setSaveState("unsaved")
        setError(reason instanceof Error ? reason.message : String(reason))
      },
    )
    saveChainRef.current = operation.then(() => undefined, () => undefined)
    return operation
  }, [applyServerPage, bridge, updatePageSummary])

  const flushDraft = useCallback(async () => {
    if (autosaveTimerRef.current !== null) {
      clearTimeout(autosaveTimerRef.current)
      autosaveTimerRef.current = null
    }
    const snapshot = editorRef.current
    if (dirtyRef.current) {
      if (!snapshot.title.trim()) {
        if (snapshot.pageId) throw new Error(t("desktop.knowledgeBases.knowledgePages.titleRequired"))
        return
      }
      await queueDraftSave(snapshot, editVersionRef.current)
      return
    }
    await saveChainRef.current
  }, [queueDraftSave, t])

  const selectPage = useCallback(async (pageId: string) => {
    const read = pageRead.current + 1
    pageRead.current = read
    setLoadingPage(true)
    setError(null)
    try {
      await flushDraft()
      const page = await bridge.getKnowledgePage(pageId)
      if (read !== pageRead.current) return
      applyServerPage(page)
    } catch (reason) {
      if (read === pageRead.current) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      if (read === pageRead.current) setLoadingPage(false)
    }
  }, [applyServerPage, bridge, flushDraft])

  useEffect(() => {
    let disposed = false
    void bridge.knowledgePages()
      .then((result) => {
        if (disposed) return
        setPages(result.pages)
        setError(null)
        if (!requestedPageId && result.selectedPageId) void selectPage(result.selectedPageId)
      })
      .catch((reason) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!disposed) setLoading(false)
      })
    return () => { disposed = true }
  }, [bridge, requestedPageId, selectPage])

  useEffect(() => {
    if (!requestedPageId) return
    const timer = window.setTimeout(() => { void selectPage(requestedPageId) }, 0)
    return () => window.clearTimeout(timer)
  }, [requestedPageId, selectPage])

  useEffect(() => {
    if (autosaveTimerRef.current !== null) clearTimeout(autosaveTimerRef.current)
    const snapshot = editorRef.current
    if (!dirtyRef.current || !snapshot.title.trim()) return
    autosaveTimerRef.current = setTimeout(() => {
      autosaveTimerRef.current = null
      void queueDraftSave(snapshot, editVersionRef.current)
    }, 500)
    return () => {
      if (autosaveTimerRef.current !== null) clearTimeout(autosaveTimerRef.current)
    }
  }, [editTick, queueDraftSave])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (autosaveTimerRef.current !== null) clearTimeout(autosaveTimerRef.current)
      const snapshot = editorRef.current
      if (dirtyRef.current && snapshot.title.trim()) {
        void queueDraftSave(snapshot, editVersionRef.current)
      }
    }
  }, [queueDraftSave])

  const updateEditor = (change: Partial<KnowledgePageEditor>) => {
    const next = {
      ...editorRef.current,
      ...change,
      verification: {
        ...editorRef.current.verification,
        canVerify: false,
        reason: "working_draft_not_verifiable" as const,
      },
    }
    editorRef.current = next
    dirtyRef.current = true
    editVersionRef.current += 1
    setEditor(next)
    setSaveState("unsaved")
    setEditTick((value) => value + 1)
  }

  const beginNew = async (kind: DesktopKnowledgePageKind) => {
    try {
      await flushDraft()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      return
    }
    pageRead.current += 1
    const next = newEditor(kind)
    editorRef.current = next
    pageIdRef.current = undefined
    dirtyRef.current = false
    setEditor(next)
    setLoadingPage(false)
    setError(null)
    setSaveState("unsaved")
    setSelectedClaim("")
  }

  const searchSources = async () => {
    if (!sourceQuery.trim()) return
    setSearchingSources(true)
    setError(null)
    try {
      setSourceResults(await bridge.searchKnowledgeSources(sourceQuery))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSearchingSources(false)
    }
  }

  const bindSource = async (source: DesktopKnowledgeSourceCandidate) => {
    if (!selectedClaim) {
      setError(t("desktop.knowledgeBases.knowledgePages.selectClaimFirst"))
      return
    }
    setBindingSource(true)
    setError(null)
    try {
      await flushDraft()
      const pageId = pageIdRef.current
      if (!pageId) throw new Error(t("desktop.knowledgeBases.knowledgePages.saveBeforeBinding"))
      const page = await bridge.bindKnowledgePageSource(
        pageId,
        selectedClaim,
        source.evidenceId,
        nextDesktopRequestId("knowledge-page-source"),
      )
      applyServerPage(page)
      updatePageSummary(page)
      setSelectedClaim("")
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBindingSource(false)
    }
  }

  const publishPage = async () => {
    setPublishing(true)
    setError(null)
    try {
      await flushDraft()
      const pageId = pageIdRef.current
      if (!pageId) throw new Error(t("desktop.knowledgeBases.knowledgePages.saveBeforePublish"))
      const page = await bridge.publishKnowledgePage(
        pageId,
        nextDesktopRequestId("knowledge-page-publish"),
      )
      applyServerPage(page)
      await refreshPages()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setPublishing(false)
    }
  }

  const verifyPage = async () => {
    const pageId = pageIdRef.current
    if (!pageId || !editorRef.current.verification.canVerify) return
    setVerifying(true)
    setError(null)
    try {
      const page = await bridge.verifyKnowledgePage(
        pageId,
        nextDesktopRequestId("knowledge-page-verification"),
      )
      applyServerPage(page)
      updatePageSummary(page)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setVerifying(false)
    }
  }

  const updateLifecycle = async (
    operation: (pageId: string) => Promise<DesktopKnowledgePage>,
  ) => {
    const pageId = pageIdRef.current
    if (!pageId) return
    setMutatingLifecycle(true)
    setError(null)
    try {
      await flushDraft()
      const page = await operation(pageId)
      applyServerPage(page)
      updatePageSummary(page)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setMutatingLifecycle(false)
    }
  }

  const permanentlyDeletePage = async () => {
    const pageId = pageIdRef.current
    if (!pageId) return
    setMutatingLifecycle(true)
    setError(null)
    try {
      await flushDraft()
      await bridge.permanentlyDeleteKnowledgePage(
        pageId,
        pageId,
        nextDesktopRequestId("knowledge-page-delete"),
      )
      pageRead.current += 1
      const next = newEditor("concept")
      editorRef.current = next
      pageIdRef.current = undefined
      dirtyRef.current = false
      setEditor(next)
      await refreshPages()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setMutatingLifecycle(false)
    }
  }

  const renderPageList = (kind: DesktopKnowledgePageKind) => {
    const group = pages.filter((page) => page.kind === kind)
    return (
      <div className="space-y-1">
        <p className="px-2 pt-3 text-[11px] font-semibold uppercase tracking-[0.15em] text-muted-foreground">
          {t(`desktop.knowledgeBases.knowledgePages.${kind}`)}
        </p>
        {group.map((page) => (
          <button
            key={page.pageId}
            type="button"
            disabled={busy}
            aria-current={editor.pageId === page.pageId ? "page" : undefined}
            onClick={() => void selectPage(page.pageId)}
            className={[
              "w-full rounded-lg px-2.5 py-2 text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
              editor.pageId === page.pageId
                ? "bg-primary text-primary-foreground shadow-sm"
                : "hover:bg-accent hover:text-accent-foreground",
            ].join(" ")}
          >
            <span className="block truncate text-sm font-medium">{page.title}</span>
            <span className="mt-0.5 block text-xs opacity-70">
              {t(`desktop.knowledgeBases.knowledgePages.state.${page.publicationState}`)}
              {page.publishedRevisionNumber
                ? ` · ${t("desktop.knowledgeBases.knowledgePages.revision", { revision: page.publishedRevisionNumber })}`
                : ""}
              {page.lifecycleState !== "draft"
                ? ` · ${t(`desktop.knowledgeBases.knowledgePages.lifecycle.state.${page.lifecycleState}`)}`
                : ""}
              {page.isStale ? ` · ${t("desktop.knowledgeBases.knowledgePages.lifecycle.stale")}` : ""}
            </span>
          </button>
        ))}
      </div>
    )
  }

  const busy = loadingPage || publishing || verifying || bindingSource || mutatingLifecycle || saveState === "saving"
  const canPublish = Boolean(editor.pageId || editor.title.trim())

  return (
    <section className="mt-8 overflow-hidden rounded-apple-lg border border-border/70 bg-background shadow-sm" data-testid="desktop-knowledge-pages">
      <div className="grid min-h-[32rem] lg:grid-cols-[15rem_minmax(0,1fr)]">
        <aside className="border-b border-border/70 bg-muted/20 p-3 lg:border-b-0 lg:border-r">
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={busy} onClick={() => void beginNew("concept")}>
              <FilePlus2 className="size-3.5" />
              {t("desktop.knowledgeBases.knowledgePages.newConcept")}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void beginNew("entity")}>
              <FilePlus2 className="size-3.5" />
              {t("desktop.knowledgeBases.knowledgePages.newEntity")}
            </Button>
          </div>
          {loading ? (
            <div className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              {t("desktop.knowledgeBases.knowledgePages.loading")}
            </div>
          ) : pages.length ? (
            <div className="mt-3">{renderPageList("concept")}{renderPageList("entity")}</div>
          ) : (
            <p className="px-2 py-6 text-sm leading-6 text-muted-foreground">
              {t("desktop.knowledgeBases.knowledgePages.empty")}
            </p>
          )}
        </aside>

        <div className="min-w-0 p-5 md:p-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-primary">
                <BookMarked className="size-4" />
                <p className="font-mono2 text-xs font-semibold tracking-[0.16em]">
                  {t(`desktop.knowledgeBases.knowledgePages.${editor.kind}`)}
                </p>
              </div>
              <h2 className="mt-2 text-xl font-semibold tracking-tight">
                {editor.pageId ? editor.title || t("desktop.knowledgeBases.knowledgePages.untitled") : t("desktop.knowledgeBases.knowledgePages.newPage")}
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {statusText(t, editor, saveState)}
              </p>
              <p className="mt-1 text-xs font-medium text-muted-foreground">
                {t(`desktop.knowledgeBases.knowledgePages.verification.state.${editor.verification.state}`)}
                {editor.verification.actor === "local_user" && editor.verification.verifiedAt
                  ? ` · ${t("desktop.knowledgeBases.knowledgePages.verification.reviewedByLocal", {
                    time: new Date(editor.verification.verifiedAt).toLocaleString(),
                  })}`
                  : ""}
              </p>
              {editor.verification.reason ? (
                <p className="mt-1 max-w-2xl text-xs text-muted-foreground">
                  {t(`desktop.knowledgeBases.knowledgePages.verification.reason.${editor.verification.reason}`)}
                </p>
              ) : null}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" disabled={busy || !editor.title.trim()} onClick={() => void flushDraft()}>
                {saveState === "saving" ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
                {t("desktop.knowledgeBases.knowledgePages.saveDraft")}
              </Button>
              <Button disabled={busy || !canPublish} onClick={() => void publishPage()}>
                {publishing ? <Loader2 className="size-4 animate-spin" /> : <Upload className="size-4" />}
                {t("desktop.knowledgeBases.knowledgePages.publish")}
              </Button>
              <Button
                variant="outline"
                disabled={busy || !editor.verification.canVerify}
                onClick={() => void verifyPage()}
              >
                {verifying ? <Loader2 className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}
                {t("desktop.knowledgeBases.knowledgePages.verification.verify")}
              </Button>
            </div>
          </div>

          {error ? <p className="mt-4 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive" role="alert">{error}</p> : null}

          <DesktopKnowledgeLifecycleControls
            key={`${editor.pageId ?? "new"}-${editor.staleAfter ?? "none"}`}
            pageId={editor.pageId}
            title={editor.title}
            lifecycleState={editor.lifecycleState}
            staleAfter={editor.staleAfter}
            isStale={editor.isStale}
            disabled={busy}
            onSetStaleAfter={(value) => updateLifecycle((pageId) => bridge.setKnowledgePageStaleAfter(
              pageId,
              value,
              nextDesktopRequestId("knowledge-page-stale-after"),
            ))}
            onDeprecate={() => updateLifecycle((pageId) => bridge.deprecateKnowledgePage(
              pageId,
              nextDesktopRequestId("knowledge-page-deprecate"),
            ))}
            onRestore={() => updateLifecycle((pageId) => bridge.restoreKnowledgePage(
              pageId,
              nextDesktopRequestId("knowledge-page-restore"),
            ))}
            onPermanentDelete={permanentlyDeletePage}
          />

          <div className="mt-6 grid gap-5 xl:grid-cols-2">
            <div className="space-y-4">
              <label className="block text-sm font-medium">
                {t("desktop.knowledgeBases.knowledgePages.kindLabel")}
                <select
                  value={editor.kind}
                  disabled={Boolean(editor.pageId) || busy}
                  onChange={(event) => updateEditor({ kind: event.target.value as DesktopKnowledgePageKind })}
                  className="mt-1.5 flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <option value="concept">{t("desktop.knowledgeBases.knowledgePages.concept")}</option>
                  <option value="entity">{t("desktop.knowledgeBases.knowledgePages.entity")}</option>
                </select>
              </label>
              <label className="block text-sm font-medium">
                {t("desktop.knowledgeBases.knowledgePages.titleLabel")}
                <Input
                  className="mt-1.5"
                  value={editor.title}
                  disabled={busy}
                  onChange={(event) => updateEditor({ title: event.target.value })}
                  placeholder={t("desktop.knowledgeBases.knowledgePages.titlePlaceholder")}
                />
              </label>
              <label className="block text-sm font-medium">
                {t("desktop.knowledgeBases.knowledgePages.markdownLabel")}
                <Textarea
                  className="mt-1.5 min-h-64 resize-y font-mono2 text-[13px] leading-6"
                  value={editor.contentMarkdown}
                  disabled={busy}
                  onChange={(event) => updateEditor({ contentMarkdown: event.target.value })}
                  onSelect={(event) => {
                    const target = event.currentTarget
                    setSelectedClaim(
                      target.value.slice(target.selectionStart, target.selectionEnd).trim(),
                    )
                  }}
                  placeholder={t("desktop.knowledgeBases.knowledgePages.markdownPlaceholder")}
                />
              </label>
            </div>
            <div className="min-w-0 rounded-xl border border-border/70 bg-muted/20 p-4">
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                {t("desktop.knowledgeBases.knowledgePages.preview")}
              </p>
              <div className="mt-3 min-h-60">
                {loadingPage ? (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />{t("desktop.knowledgeBases.knowledgePages.loading")}</div>
                ) : editor.contentMarkdown ? (
                  <MarkdownView source={editor.contentMarkdown} />
                ) : (
                  <p className="text-sm text-muted-foreground">{t("desktop.knowledgeBases.knowledgePages.previewEmpty")}</p>
                )}
              </div>
            </div>
          </div>

          <div className="mt-5 grid gap-4 rounded-xl border border-border/70 bg-muted/15 p-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
            <div>
              <div className="flex items-center gap-2">
                <Link2 className="size-4 text-primary" />
                <h3 className="text-sm font-semibold">
                  {t("desktop.knowledgeBases.knowledgePages.sourceBinding")}
                </h3>
              </div>
              <p className="mt-2 text-sm text-muted-foreground">
                {selectedClaim
                  ? t("desktop.knowledgeBases.knowledgePages.selectedClaim", { claim: selectedClaim })
                  : t("desktop.knowledgeBases.knowledgePages.selectClaim")}
              </p>
              <p className="mt-2 text-xs font-medium text-muted-foreground">
                {t(`desktop.knowledgeBases.knowledgePages.provenance.${editor.provenanceState}`)}
              </p>
              <div className="mt-3 flex gap-2">
                <Input
                  value={sourceQuery}
                  onChange={(event) => setSourceQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault()
                      void searchSources()
                    }
                  }}
                  placeholder={t("desktop.knowledgeBases.knowledgePages.sourceSearchPlaceholder")}
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={searchingSources || !sourceQuery.trim()}
                  onClick={() => void searchSources()}
                >
                  {searchingSources ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
                  {t("desktop.knowledgeBases.knowledgePages.searchSources")}
                </Button>
              </div>
              {editor.sourceMap.length ? (
                <div className="mt-3 space-y-2">
                  {editor.sourceMap.map((source) => (
                    <div key={source.sourceId} className="rounded-lg border border-border/60 bg-background px-3 py-2 text-xs">
                      <p className="font-medium">{source.documentName} · {source.section}</p>
                      <p className="mt-1 text-muted-foreground">
                        {source.sourceId} · {t(`desktop.knowledgeBases.knowledgePages.sourceAvailability.${source.availability}`)}
                      </p>
                    </div>
                  ))}
                </div>
              ) : null}
              {editor.publicationDiagnostics.length ? (
                <div className="mt-3 space-y-2" role="alert">
                  {editor.publicationDiagnostics.map((diagnostic) => (
                    <p key={`${diagnostic.code}-${diagnostic.sourceId}`} className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                      {diagnostic.message}
                    </p>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="max-h-72 space-y-2 overflow-y-auto">
              {sourceResults.length ? sourceResults.map((source) => (
                <button
                  key={source.evidenceId}
                  type="button"
                  disabled={bindingSource || !selectedClaim}
                  onClick={() => void bindSource(source)}
                  className="block w-full rounded-lg border border-border/70 bg-background px-3 py-2.5 text-left transition-colors hover:border-primary/50 hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span className="block text-sm font-medium">{source.documentName}</span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">{source.section}</span>
                  <span className="mt-2 line-clamp-3 block text-xs leading-5">{source.excerpt}</span>
                </button>
              )) : (
                <p className="py-5 text-center text-sm text-muted-foreground">
                  {t("desktop.knowledgeBases.knowledgePages.sourceSearchEmpty")}
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

function editorFromPage(page: DesktopKnowledgePage): KnowledgePageEditor {
  const editable = page.workingDraft ?? page.publishedRevision
  return {
    pageId: page.pageId,
    kind: page.kind,
    title: editable?.title ?? page.title,
    contentMarkdown: editable?.contentMarkdown ?? "",
    publicationState: page.publicationState,
    provenanceState: editable?.provenanceState ?? "structural",
    verification: page.verification,
    publishedRevisionNumber: page.publishedRevisionNumber,
    lifecycleState: page.lifecycleState,
    staleAfter: page.staleAfter,
    isStale: page.isStale,
    sourceMap: editable?.sourceMap ?? [],
    publicationDiagnostics: page.publicationDiagnostics,
  }
}

function statusText(
  t: (key: string, options?: Record<string, unknown>) => string,
  editor: KnowledgePageEditor,
  saveState: DraftSaveState,
): string {
  if (saveState === "saving") return t("desktop.knowledgeBases.knowledgePages.draftSaving")
  if (saveState === "unsaved") return t("desktop.knowledgeBases.knowledgePages.unsaved")
  if (saveState === "saved") return t("desktop.knowledgeBases.knowledgePages.draftSaved")
  return t("desktop.knowledgeBases.knowledgePages.publishedStatus", {
    revision: editor.publishedRevisionNumber ?? 0,
  })
}
