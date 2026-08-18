import { BookMarked, FilePlus2, Loader2, Save } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import MarkdownView from "@/components/MarkdownView"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { useDesktopBridge } from "./bridge-context"
import { nextDesktopRequestId } from "./request-id"
import type {
  DesktopKnowledgePage,
  DesktopKnowledgePageKind,
  DesktopKnowledgePageSummary,
} from "./contracts"

type KnowledgePageDraft = {
  pageId: string | undefined
  kind: DesktopKnowledgePageKind
  title: string
  contentMarkdown: string
  revisionNumber: number | undefined
}

function newDraft(kind: DesktopKnowledgePageKind): KnowledgePageDraft {
  return { pageId: undefined, kind, title: "", contentMarkdown: "", revisionNumber: undefined }
}

/** Browse and revise SQLite-authoritative Concept and Entity pages. */
export function DesktopKnowledgePagePanel({ requestedPageId }: { requestedPageId?: string | null }) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [pages, setPages] = useState<DesktopKnowledgePageSummary[]>([])
  const [draft, setDraft] = useState<KnowledgePageDraft>(() => newDraft("concept"))
  const [loading, setLoading] = useState(true)
  const [loadingPage, setLoadingPage] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const pageRead = useRef(0)

  const refreshPages = useCallback(async () => {
    const result = await bridge.knowledgePages()
    setPages(result.pages)
  }, [bridge])

  useEffect(() => {
    let disposed = false
    void bridge.knowledgePages()
      .then((result) => {
        if (disposed) return
        setPages(result.pages)
        setError(null)
      })
      .catch((reason) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!disposed) setLoading(false)
      })
    return () => { disposed = true }
  }, [bridge])

  const selectPage = useCallback(async (pageId: string) => {
    const read = pageRead.current + 1
    pageRead.current = read
    setLoadingPage(true)
    setError(null)
    setSaved(false)
    try {
      const page = await bridge.getKnowledgePage(pageId)
      if (read !== pageRead.current) return
      setDraft(draftFromPage(page))
    } catch (reason) {
      if (read === pageRead.current) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      if (read === pageRead.current) setLoadingPage(false)
    }
  }, [bridge])

  useEffect(() => {
    if (requestedPageId) {
      void Promise.resolve().then(() => selectPage(requestedPageId))
    }
  }, [requestedPageId, selectPage])

  const beginNew = (kind: DesktopKnowledgePageKind) => {
    pageRead.current += 1
    setDraft(newDraft(kind))
    setLoadingPage(false)
    setError(null)
    setSaved(false)
  }

  const savePage = async () => {
    if (!draft.title.trim()) {
      setError(t("desktop.knowledgeBases.knowledgePages.titleRequired"))
      return
    }
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const page = await bridge.saveKnowledgePage(
        draft.pageId,
        draft.kind,
        draft.title,
        draft.contentMarkdown,
        nextDesktopRequestId("knowledge-page"),
      )
      setDraft(draftFromPage(page))
      await refreshPages()
      setSaved(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSaving(false)
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
            aria-current={draft.pageId === page.pageId ? "page" : undefined}
            onClick={() => void selectPage(page.pageId)}
            className={[
              "w-full rounded-lg px-2.5 py-2 text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
              draft.pageId === page.pageId
                ? "bg-primary text-primary-foreground shadow-sm"
                : "hover:bg-accent hover:text-accent-foreground",
            ].join(" ")}
          >
            <span className="block truncate text-sm font-medium">{page.title}</span>
            <span className="mt-0.5 block text-xs opacity-70">
              {t("desktop.knowledgeBases.knowledgePages.revision", { revision: page.revisionNumber })}
            </span>
          </button>
        ))}
      </div>
    )
  }

  return (
    <section className="mt-8 overflow-hidden rounded-apple-lg border border-border/70 bg-background shadow-sm" data-testid="desktop-knowledge-pages">
      <div className="grid min-h-[32rem] lg:grid-cols-[15rem_minmax(0,1fr)]">
        <aside className="border-b border-border/70 bg-muted/20 p-3 lg:border-b-0 lg:border-r">
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => beginNew("concept")}>
              <FilePlus2 className="size-3.5" />
              {t("desktop.knowledgeBases.knowledgePages.newConcept")}
            </Button>
            <Button size="sm" variant="outline" onClick={() => beginNew("entity")}>
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
                  {t(`desktop.knowledgeBases.knowledgePages.${draft.kind}`)}
                </p>
              </div>
              <h2 className="mt-2 text-xl font-semibold tracking-tight">
                {draft.pageId ? draft.title || t("desktop.knowledgeBases.knowledgePages.untitled") : t("desktop.knowledgeBases.knowledgePages.newPage")}
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {draft.revisionNumber === undefined
                  ? t("desktop.knowledgeBases.knowledgePages.newDescription")
                  : t("desktop.knowledgeBases.knowledgePages.authorityDescription", { revision: draft.revisionNumber })}
              </p>
            </div>
            <Button disabled={saving || loadingPage} onClick={() => void savePage()}>
              {saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
              {saving ? t("desktop.knowledgeBases.knowledgePages.saving") : t("desktop.knowledgeBases.knowledgePages.save")}
            </Button>
          </div>

          {error ? <p className="mt-4 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive" role="alert">{error}</p> : null}
          {saved ? <p className="mt-4 rounded-lg border border-emerald-600/25 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">{t("desktop.knowledgeBases.knowledgePages.saved")}</p> : null}

          <div className="mt-6 grid gap-5 xl:grid-cols-2">
            <div className="space-y-4">
              <label className="block text-sm font-medium">
                {t("desktop.knowledgeBases.knowledgePages.kindLabel")}
                <select
                  value={draft.kind}
                  disabled={Boolean(draft.pageId) || loadingPage || saving}
                  onChange={(event) => setDraft((current) => ({ ...current, kind: event.target.value as DesktopKnowledgePageKind }))}
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
                  value={draft.title}
                  disabled={loadingPage || saving}
                  onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))}
                  placeholder={t("desktop.knowledgeBases.knowledgePages.titlePlaceholder")}
                />
              </label>
              <label className="block text-sm font-medium">
                {t("desktop.knowledgeBases.knowledgePages.markdownLabel")}
                <Textarea
                  className="mt-1.5 min-h-64 resize-y font-mono2 text-[13px] leading-6"
                  value={draft.contentMarkdown}
                  disabled={loadingPage || saving}
                  onChange={(event) => setDraft((current) => ({ ...current, contentMarkdown: event.target.value }))}
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
                ) : draft.contentMarkdown ? (
                  <MarkdownView source={draft.contentMarkdown} />
                ) : (
                  <p className="text-sm text-muted-foreground">{t("desktop.knowledgeBases.knowledgePages.previewEmpty")}</p>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

function draftFromPage(page: DesktopKnowledgePage): KnowledgePageDraft {
  return {
    pageId: page.pageId,
    kind: page.kind,
    title: page.title,
    contentMarkdown: page.contentMarkdown,
    revisionNumber: page.revisionNumber,
  }
}
