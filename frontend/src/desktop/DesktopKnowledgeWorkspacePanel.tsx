import { BookOpen, CopyPlus, FilePlus2, History, Loader2, Search, Sparkles, UserRoundPen } from "lucide-react"
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import MarkdownView from "@/components/MarkdownView"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useDesktopBridge } from "./bridge-context"
import type {
  DesktopGeneratedKnowledgeItem,
  DesktopKnowledgeAdoptionDecision,
  DesktopKnowledgeAdoptionResult,
  DesktopKnowledgeGenerationSummary,
  DesktopKnowledgeWorkspaceItem,
  DesktopKnowledgeWorkspaceItemSummary,
} from "./contracts"
import { DesktopKnowledgePagePanel } from "./DesktopKnowledgePagePanel"
import {
  knowledgeWorkspaceRequestIsCurrent,
  reloadKnowledgeWorkspaceAfterUserMutation,
} from "./knowledge-workspace-refresh"
import { nextDesktopRequestId } from "./request-id"

/** Browse generated snapshots and user-owned pages through one additive read surface. */
export function DesktopKnowledgeWorkspacePanel({
  requestedPageId,
}: {
  requestedPageId?: string | null
}) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [query, setQuery] = useState("")
  const [items, setItems] = useState<DesktopKnowledgeWorkspaceItemSummary[]>([])
  const [selected, setSelected] = useState<DesktopKnowledgeWorkspaceItemSummary | null>(null)
  const [detail, setDetail] = useState<DesktopKnowledgeWorkspaceItem | null>(null)
  const [generations, setGenerations] = useState<DesktopKnowledgeGenerationSummary[] | null>(null)
  const [historicalGenerationId, setHistoricalGenerationId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [adopting, setAdopting] = useState(false)
  const [adoption, setAdoption] = useState<DesktopKnowledgeAdoptionResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [creatingPage, setCreatingPage] = useState(false)
  const currentRequestSequence = useRef(0)
  const queryRef = useRef(query)
  const requestedPageIdRef = useRef(requestedPageId)
  const pendingPreferredPageIdRef = useRef<string | null>(null)

  useLayoutEffect(() => {
    queryRef.current = query
    requestedPageIdRef.current = requestedPageId
  }, [query, requestedPageId])

  const loadCurrent = useCallback(async (
    search: string,
    preferredPageId?: string,
    requestedSequence?: number,
  ) => {
    const requestSequence = requestedSequence ?? ++currentRequestSequence.current
    const result = await bridge.knowledgeWorkspace(search)
    if (!knowledgeWorkspaceRequestIsCurrent(
      requestSequence,
      currentRequestSequence.current,
    )) return "stale" as const
    const preferred = preferredPageId
      ? result.items.find((item) => item.authority === "user" && item.pageId === preferredPageId)
      : undefined
    if (preferredPageId && !preferred) return "preferred_missing" as const
    setHistoricalGenerationId(null)
    setItems(result.items)
    setCreatingPage(false)
    setSelected((current) => {
      const latestRequestedPageId = requestedPageIdRef.current
      const requested = preferred ?? (latestRequestedPageId
        ? result.items.find((item) => item.authority === "user" && item.pageId === latestRequestedPageId)
        : undefined
      )
      return requested
        ?? result.items.find((item) => item.identity === current?.identity)
        ?? result.items[0]
        ?? null
    })
    return "loaded" as const
  }, [bridge])

  const reloadAfterUserMutation = useCallback((preferredPageId: string | null) => {
    setError(null)
    const requestSequence = ++currentRequestSequence.current
    void reloadKnowledgeWorkspaceAfterUserMutation(
      loadCurrent,
      queryRef.current,
      preferredPageId,
      requestSequence,
    )
      .catch((reason) => {
        if (knowledgeWorkspaceRequestIsCurrent(
          requestSequence,
          currentRequestSequence.current,
        )) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      })
      .finally(() => {
        if (knowledgeWorkspaceRequestIsCurrent(
          requestSequence,
          currentRequestSequence.current,
        )) setLoading(false)
      })
  }, [loadCurrent])

  useEffect(() => {
    let disposed = false
    const requestSequence = ++currentRequestSequence.current
    const preferredPageId = pendingPreferredPageIdRef.current
    const timer = window.setTimeout(() => {
      const load = preferredPageId
        ? loadCurrent(query, preferredPageId, requestSequence)
        : reloadKnowledgeWorkspaceAfterUserMutation(
            loadCurrent,
            query,
            null,
            requestSequence,
          )
      void load.then((outcome) => {
        if (knowledgeWorkspaceRequestIsCurrent(
          requestSequence,
          currentRequestSequence.current,
        )) {
          pendingPreferredPageIdRef.current = null
          if (outcome === "preferred_missing") {
            setError(t("desktop.knowledge.workspace.candidateUnavailable"))
          }
        }
      }).catch((reason) => {
        if (!disposed && knowledgeWorkspaceRequestIsCurrent(
          requestSequence,
          currentRequestSequence.current,
        )) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      }).finally(() => {
        if (!disposed && knowledgeWorkspaceRequestIsCurrent(
          requestSequence,
          currentRequestSequence.current,
        )) setLoading(false)
      })
    }, query ? 180 : 0)
    return () => {
      disposed = true
      if (currentRequestSequence.current === requestSequence) {
        currentRequestSequence.current += 1
      }
      window.clearTimeout(timer)
    }
  }, [loadCurrent, query, t])

  useEffect(() => {
    if (!selected) return
    let disposed = false
    void bridge.getKnowledgeWorkspaceItem(selected)
      .then((item) => {
        if (!disposed) setDetail(item)
      })
      .catch((reason) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { disposed = true }
  }, [bridge, selected])

  const grouped = useMemo(() => ({
    concept: items.filter((item) => item.kind === "concept"),
    entity: items.filter((item) => item.kind === "entity"),
    procedure: items.filter((item) => item.kind === "procedure"),
  }), [items])
  const selectedDetail = detail?.identity === selected?.identity ? detail : null
  const selectedAdoption = selectedDetail?.authority === "generated"
    && adoption?.generationId === selectedDetail.generationId
    && adoption.itemKey === selectedDetail.itemKey
    ? adoption
    : null

  const toggleHistory = async () => {
    const requestSequence = ++currentRequestSequence.current
    pendingPreferredPageIdRef.current = null
    setCreatingPage(false)
    if (generations !== null) {
      setGenerations(null)
      setLoading(true)
      setError(null)
      try {
        await loadCurrent(queryRef.current, undefined, requestSequence)
      } catch (reason) {
        if (knowledgeWorkspaceRequestIsCurrent(
          requestSequence,
          currentRequestSequence.current,
        )) setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        if (knowledgeWorkspaceRequestIsCurrent(
          requestSequence,
          currentRequestSequence.current,
        )) setLoading(false)
      }
      return
    }
    setLoading(false)
    setError(null)
    try {
      const history = await bridge.knowledgeWorkspaceHistory()
      if (knowledgeWorkspaceRequestIsCurrent(
        requestSequence,
        currentRequestSequence.current,
      )) setGenerations(history.generations ?? [])
    } catch (reason) {
      if (knowledgeWorkspaceRequestIsCurrent(
        requestSequence,
        currentRequestSequence.current,
      )) setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const openGeneration = async (generationId: number) => {
    const requestSequence = ++currentRequestSequence.current
    pendingPreferredPageIdRef.current = null
    setCreatingPage(false)
    setLoading(true)
    setError(null)
    try {
      const history = await bridge.knowledgeWorkspaceHistory(generationId)
      if (!knowledgeWorkspaceRequestIsCurrent(
        requestSequence,
        currentRequestSequence.current,
      )) return
      const historicalItems = history.items ?? []
      setHistoricalGenerationId(generationId)
      setItems(historicalItems)
      setSelected(historicalItems[0] ?? null)
    } catch (reason) {
      if (knowledgeWorkspaceRequestIsCurrent(
        requestSequence,
        currentRequestSequence.current,
      )) setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      if (knowledgeWorkspaceRequestIsCurrent(
        requestSequence,
        currentRequestSequence.current,
      )) setLoading(false)
    }
  }

  const adopt = async (
    item: DesktopGeneratedKnowledgeItem,
    decision?: DesktopKnowledgeAdoptionDecision,
    candidatePageId?: string,
  ) => {
    setAdopting(true)
    setError(null)
    try {
      const result = await bridge.adoptKnowledgeItem(
        item.generationId,
        item.itemKey,
        nextDesktopRequestId("knowledge-adoption"),
        nextDesktopRequestId("knowledge-adoption-command"),
        decision,
        candidatePageId,
      )
      setAdoption(result)
      if (result.pageId) {
        if (await loadCurrent(queryRef.current, result.pageId) === "loaded") {
          toast.success(t("desktop.knowledge.workspace.adopted"))
        }
      } else if (result.status === "reconciliation_required") {
        toast.success(t("desktop.knowledge.workspace.reconciliationQueued"))
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setAdopting(false)
    }
  }

  const selectAdoptionCandidate = (pageId: string) => {
    setError(null)
    setGenerations(null)
    pendingPreferredPageIdRef.current = pageId
    if (queryRef.current) {
      setQuery("")
      return
    }
    const requestSequence = ++currentRequestSequence.current
    setLoading(true)
    void loadCurrent("", pageId, requestSequence).then((outcome) => {
      if (knowledgeWorkspaceRequestIsCurrent(
        requestSequence,
        currentRequestSequence.current,
      )) {
        pendingPreferredPageIdRef.current = null
        if (outcome === "preferred_missing") {
          setError(t("desktop.knowledge.workspace.candidateUnavailable"))
        }
      }
    }).catch((reason) => {
      if (knowledgeWorkspaceRequestIsCurrent(
        requestSequence,
        currentRequestSequence.current,
      )) setError(reason instanceof Error ? reason.message : String(reason))
    }).finally(() => {
      if (knowledgeWorkspaceRequestIsCurrent(
        requestSequence,
        currentRequestSequence.current,
      )) setLoading(false)
    })
  }

  const beginNewPage = () => {
    currentRequestSequence.current += 1
    pendingPreferredPageIdRef.current = null
    setLoading(false)
    setGenerations(null)
    setHistoricalGenerationId(null)
    setSelected(null)
    setDetail(null)
    setCreatingPage(true)
    setError(null)
  }

  return (
    <section className="mt-5 overflow-hidden rounded-apple-lg border border-border/70 bg-background shadow-sm" data-testid="knowledge-workspace-browser">
      <div className="grid min-h-[34rem] lg:grid-cols-[18rem_minmax(0,1fr)]">
        <aside className="border-b border-border/70 bg-muted/20 p-3 lg:border-b-0 lg:border-r">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
            <Input
              className="pl-8"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("desktop.knowledge.workspace.search")}
            />
          </div>
          <Button className="mt-2 w-full justify-start" size="sm" onClick={beginNewPage}>
            <FilePlus2 className="size-4" />
            {t("desktop.knowledgeBases.knowledgePages.newPage")}
          </Button>
          <Button className="mt-2 w-full justify-start" size="sm" variant="outline" onClick={() => void toggleHistory()}>
            <History className="size-4" />
            {generations === null
              ? t("desktop.knowledge.workspace.history")
              : t("desktop.knowledge.workspace.current")}
          </Button>
          {generations !== null ? (
            <div className="mt-2 space-y-1 rounded-lg border border-border/60 bg-background/70 p-1.5">
              {generations.length ? generations.map((generation) => (
                <button
                  type="button"
                  key={generation.generationId}
                  onClick={() => void openGeneration(generation.generationId)}
                  className="w-full rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent"
                >
                  <span className="block font-medium">
                    {t("desktop.knowledge.workspace.generation", { id: generation.generationId })}
                    {generation.current ? ` · ${t("desktop.knowledge.workspace.currentBadge")}` : ""}
                  </span>
                  <span className="text-muted-foreground">
                    {t("desktop.knowledge.workspace.itemCount", { count: generation.itemCount })}
                  </span>
                </button>
              )) : <p className="px-2 py-3 text-xs text-muted-foreground">{t("desktop.knowledge.workspace.noHistory")}</p>}
            </div>
          ) : null}
          {historicalGenerationId !== null ? (
            <p className="mt-2 rounded-md bg-amber-500/10 px-2 py-1.5 text-xs text-amber-800 dark:text-amber-200">
              {t("desktop.knowledge.workspace.historical", { id: historicalGenerationId })}
            </p>
          ) : null}
          {loading ? (
            <p className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />{t("desktop.knowledge.workspace.loading")}</p>
          ) : items.length ? (
            (["concept", "entity", "procedure"] as const).map((kind) => grouped[kind].length ? (
              <div className="mt-3 space-y-1" key={kind}>
                <p className="px-2 text-[11px] font-semibold uppercase tracking-[0.15em] text-muted-foreground">{t(`desktop.knowledgeBases.knowledgePages.${kind}`)}</p>
                {grouped[kind].map((item) => (
                  <button
                    type="button"
                    key={item.identity}
                    aria-current={selected?.identity === item.identity ? "page" : undefined}
                    onClick={() => {
                      currentRequestSequence.current += 1
                      pendingPreferredPageIdRef.current = null
                      setLoading(false)
                      setCreatingPage(false)
                      setSelected(item)
                    }}
                    className={[
                      "w-full rounded-lg px-2.5 py-2 text-left transition-colors",
                      selected?.identity === item.identity ? "bg-primary text-primary-foreground" : "hover:bg-accent",
                    ].join(" ")}
                  >
                    <span className="block truncate text-sm font-medium">{item.title}</span>
                    <span className="mt-1 flex flex-wrap gap-1">
                      <Badge variant="outline" className="text-[10px]">{t(`desktop.knowledge.workspace.authority.${item.authority}`)}</Badge>
                      {item.authority === "generated" ? <Badge variant="outline" className="text-[10px]">{item.current ? t("desktop.knowledge.workspace.currentBadge") : t("desktop.knowledge.workspace.historyBadge")}</Badge> : null}
                      {item.authority === "user" ? <Badge variant="outline" className="text-[10px]">{t(`desktop.knowledgeBases.knowledgePages.state.${item.publicationState}`)}</Badge> : null}
                    </span>
                  </button>
                ))}
              </div>
            ) : null)
          ) : (
            <p className="px-2 py-6 text-sm leading-6 text-muted-foreground">{t("desktop.knowledge.workspace.empty")}</p>
          )}
        </aside>
        <div className="min-w-0 p-4 md:p-5">
          {error ? <p className="mb-4 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive" role="alert">{error}</p> : null}
          {selected && !selectedDetail ? (
            <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />{t("desktop.knowledge.workspace.loadingItem")}</p>
          ) : selectedDetail?.authority === "generated" ? (
            <GeneratedKnowledgeDetail
              item={selectedDetail}
              adopting={adopting}
              adoption={selectedAdoption}
              onAdopt={() => void adopt(selectedDetail)}
              onCreateNew={() => void adopt(selectedDetail, "create_new")}
              onReconcileCandidate={(pageId) => void adopt(
                selectedDetail,
                "use_existing",
                pageId,
              )}
              onSelectCandidate={(pageId) => void selectAdoptionCandidate(pageId)}
            />
          ) : selected?.authority === "user" && selected.pageId ? (
            <DesktopKnowledgePagePanel
              key={selected.pageId}
              requestedPageId={selected.pageId}
              onKnowledgePagesChanged={reloadAfterUserMutation}
              embedded
            />
          ) : (
            <div>
              <p className="flex items-center gap-2 text-sm font-medium"><BookOpen className="size-4 text-primary" />{t("desktop.knowledge.workspace.createOrSelect")}</p>
              <DesktopKnowledgePagePanel
                key={creatingPage ? "new-page" : "empty-workspace"}
                onKnowledgePagesChanged={reloadAfterUserMutation}
                embedded
                startNew
              />
            </div>
          )}
        </div>
      </div>
    </section>
  )
}

function GeneratedKnowledgeDetail({
  item,
  adopting,
  adoption,
  onAdopt,
  onCreateNew,
  onReconcileCandidate,
  onSelectCandidate,
}: {
  item: DesktopGeneratedKnowledgeItem
  adopting: boolean
  adoption: DesktopKnowledgeAdoptionResult | null
  onAdopt: () => void
  onCreateNew: () => void
  onReconcileCandidate: (pageId: string) => void
  onSelectCandidate: (pageId: string) => void
}) {
  const { t } = useTranslation("common")
  return (
    <article data-testid="generated-knowledge-detail">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.14em] text-primary"><Sparkles className="size-4" />{t("desktop.knowledge.workspace.generatedSnapshot")}</p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight">{item.title}</h2>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <Badge>{t(`desktop.knowledgeBases.knowledgePages.${item.kind}`)}</Badge>
            <Badge variant="outline">{item.current ? t("desktop.knowledge.workspace.currentBadge") : t("desktop.knowledge.workspace.historyBadge")}</Badge>
            <Badge variant="outline">{t(`desktop.knowledgeBases.knowledgePages.provenance.${item.provenanceState}`)}</Badge>
          </div>
        </div>
        <Button disabled={adopting} onClick={onAdopt}>
          {adopting ? <Loader2 className="size-4 animate-spin" /> : <CopyPlus className="size-4" />}
          {t("desktop.knowledge.workspace.adopt")}
        </Button>
      </div>
      <p className="mt-3 text-xs text-muted-foreground">{t("desktop.knowledge.workspace.readOnly")}</p>
      {item.aliases.length || item.identityLabels.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5">{[...item.aliases, ...item.identityLabels].map((label) => <Badge variant="secondary" key={label}>{label}</Badge>)}</div>
      ) : null}
      {adoption && !adoption.pageId ? (
        <div className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-100">
          <p className="font-medium">{t(`desktop.knowledge.workspace.adoption.${adoption.status}`)}</p>
          {adoption.candidates.map((candidate) => (
            <Button
              className="mt-2 mr-2"
              disabled={adopting}
              size="sm"
              variant="outline"
              key={candidate.pageId}
              onClick={() => adoption.status === "choice_required"
                ? onReconcileCandidate(candidate.pageId)
                : onSelectCandidate(candidate.pageId)}
            >
              <UserRoundPen className="size-3.5" />
              {adoption.status === "choice_required"
                ? t("desktop.knowledge.workspace.reconcileCandidate", { title: candidate.title })
                : t("desktop.knowledge.workspace.openCandidate", { title: candidate.title })}
            </Button>
          ))}
          {adoption.status === "choice_required" ? (
            <Button className="mt-2" disabled={adopting} size="sm" onClick={onCreateNew}>
              <CopyPlus className="size-3.5" />{t("desktop.knowledge.workspace.createSeparate")}
            </Button>
          ) : null}
        </div>
      ) : null}
      <div className="mt-6 rounded-xl border border-border/70 bg-muted/15 p-5 text-sm leading-7"><MarkdownView source={item.contentMarkdown} /></div>
      <section className="mt-5">
        <h3 className="text-sm font-semibold">{t("desktop.knowledge.workspace.sources")}</h3>
        {item.sourceMap.length ? (
          <div className="mt-2 space-y-2">{item.sourceMap.map((source) => (
            <div className="rounded-lg border border-border/60 px-3 py-2 text-xs" key={`${source.sourceId}-${source.claimText}`}>
              <p className="font-medium">{source.documentName} · {source.section}</p>
              <p className="mt-1 line-clamp-3 text-muted-foreground">{source.excerpt}</p>
            </div>
          ))}</div>
        ) : <p className="mt-2 text-sm text-muted-foreground">{t("desktop.knowledge.workspace.noSources")}</p>}
      </section>
    </article>
  )
}
