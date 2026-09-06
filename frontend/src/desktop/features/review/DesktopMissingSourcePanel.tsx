import { AlertTriangle, FileText, Link2, Loader2, Search, Trash2 } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useDesktopBridge } from "@/desktop/bridge/context"
import type {
  DesktopKnowledgeSourceCandidate,
  DesktopMissingSourceCandidate,
} from "@/desktop/bridge/contracts"
import { nextDesktopRequestId } from "@/desktop/shared/request-id"

/** Reviews factual claims that model analysis could not connect to Available Evidence. */
export function DesktopMissingSourcePanel({ onResolved }: { onResolved?: () => void }) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [candidates, setCandidates] = useState<DesktopMissingSourceCandidate[]>([])
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [bindingId, setBindingId] = useState<string | null>(null)
  const [sourceQuery, setSourceQuery] = useState("")
  const [sourceResults, setSourceResults] = useState<DesktopKnowledgeSourceCandidate[]>([])
  const [hasSearched, setHasSearched] = useState(false)
  const [loading, setLoading] = useState(true)
  const [searching, setSearching] = useState(false)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  useEffect(() => {
    let disposed = false
    void bridge.missingSourceCandidates()
      .then((result) => {
        if (disposed) return
        setCandidates(result.candidates)
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

  const selectedCandidates = useMemo(
    () => candidates.filter((candidate) => selectedIds.includes(candidate.candidateId)),
    [candidates, selectedIds],
  )
  const allSelected = Boolean(candidates.length) && selectedCandidates.length === candidates.length

  const removeResolved = (candidateIds: string[]) => {
    const resolved = new Set(candidateIds)
    setCandidates((current) => current.filter((candidate) => !resolved.has(candidate.candidateId)))
    setSelectedIds((current) => current.filter((candidateId) => !resolved.has(candidateId)))
    if (bindingId && resolved.has(bindingId)) {
      setBindingId(null)
      setSourceQuery("")
      setSourceResults([])
      setHasSearched(false)
    }
    onResolved?.()
  }

  const openBinding = (candidate: DesktopMissingSourceCandidate) => {
    setBindingId(candidate.candidateId)
    setSourceQuery(candidate.claimText)
    setSourceResults([])
    setHasSearched(false)
    setSaved(null)
    setError(null)
  }

  const searchSources = async () => {
    if (!sourceQuery.trim()) return
    setSearching(true)
    setError(null)
    try {
      setSourceResults(await bridge.searchKnowledgeSources(sourceQuery))
      setHasSearched(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSearching(false)
    }
  }

  const bindSource = async (
    candidate: DesktopMissingSourceCandidate,
    source: DesktopKnowledgeSourceCandidate,
  ) => {
    setWorking(true)
    setError(null)
    setSaved(null)
    try {
      const result = await bridge.bindMissingSourceCandidate(
        candidate.candidateId,
        source.evidenceId,
        nextDesktopRequestId("missing-source-bind"),
      )
      removeResolved([candidate.candidateId])
      setSaved(t(`desktop.knowledgeBases.missingSource.outcomes.${result.outcome}`))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setWorking(false)
    }
  }

  const dismissSelected = async () => {
    if (!selectedIds.length) return
    setWorking(true)
    setError(null)
    setSaved(null)
    try {
      const result = await bridge.dismissMissingSourceCandidates(
        selectedIds,
        nextDesktopRequestId("missing-source-dismiss"),
      )
      removeResolved(result.resolvedCandidateIds)
      setSaved(t("desktop.knowledgeBases.missingSource.dismissed", {
        count: result.resolvedCandidateIds.length,
      }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setWorking(false)
    }
  }

  const toggleSelected = (candidateId: string) => {
    setSelectedIds((current) => (
      current.includes(candidateId)
        ? current.filter((value) => value !== candidateId)
        : [...current, candidateId]
    ))
  }

  return (
    <section className="mt-8 max-w-4xl" data-testid="desktop-missing-source-candidates">
      <div className="rounded-apple-lg border border-border/70 bg-background p-5 shadow-sm md:p-6">
        <div className="flex items-start gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-orange-500/10 text-orange-700 dark:text-orange-300">
            <AlertTriangle className="size-4" />
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-semibold">{t("desktop.knowledgeBases.missingSource.title")}</h2>
              <span className="rounded-full bg-orange-500/10 px-2 py-0.5 text-[11px] font-semibold text-orange-700 dark:text-orange-300">
                {t("desktop.knowledgeBases.missingSource.category")}
              </span>
            </div>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              {t("desktop.knowledgeBases.missingSource.description")}
            </p>
          </div>
        </div>

        {error ? (
          <p className="mt-5 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : null}
        {saved ? (
          <p className="mt-5 rounded-lg border border-emerald-600/25 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">
            {saved}
          </p>
        ) : null}

        {loading ? (
          <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("desktop.knowledgeBases.missingSource.loading")}
          </div>
        ) : candidates.length ? (
          <>
            <div className="mt-6 flex flex-wrap items-center gap-2 rounded-xl border border-border/70 bg-muted/20 p-3">
              <label className="flex items-center gap-2 text-sm text-muted-foreground">
                <input
                  type="checkbox"
                  checked={allSelected}
                  disabled={working || searching}
                  onChange={() => setSelectedIds(allSelected ? [] : candidates.map((item) => item.candidateId))}
                />
                {t("desktop.knowledgeBases.missingSource.selectAll")}
              </label>
              <span className="text-xs text-muted-foreground">
                {t("desktop.knowledgeBases.missingSource.selectedCount", { count: selectedCandidates.length })}
              </span>
              <Button
                className="ml-auto"
                size="sm"
                variant="outline"
                disabled={working || searching || !selectedIds.length}
                onClick={() => void dismissSelected()}
              >
                {working ? <Loader2 className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
                {t("desktop.knowledgeBases.missingSource.dismissSelected")}
              </Button>
            </div>

            <div className="mt-4 space-y-3">
              {candidates.map((candidate) => (
                <article key={candidate.candidateId} className="rounded-xl border border-border/70 bg-muted/20 p-4">
                  <div className="flex items-start gap-3">
                    <input
                      className="mt-1"
                      type="checkbox"
                      checked={selectedIds.includes(candidate.candidateId)}
                      disabled={working || searching}
                      onChange={() => toggleSelected(candidate.candidateId)}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-medium">{candidate.title}</p>
                        <span className="rounded-full border border-border/70 px-2 py-0.5 text-[11px] text-muted-foreground">
                          {t(`desktop.knowledgeBases.knowledgePages.${candidate.kind}`)}
                        </span>
                      </div>
                      <blockquote className="mt-3 border-l-2 border-orange-500/50 pl-3 text-sm leading-6">
                        {candidate.claimText}
                      </blockquote>
                      <p className="mt-3 text-xs text-orange-700 dark:text-orange-300">
                        {t(`desktop.knowledgeBases.missingSource.reasons.${candidate.reason}`)}
                      </p>
                      <p className="mt-2 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                        <FileText className="size-3.5" />
                        <span>{candidate.documentName}</span>
                        <span>·</span>
                        <span>{candidate.section || t("desktop.knowledgeBases.missingSource.documentLevel")}</span>
                      </p>
                      <div className="mt-4 flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          disabled={working || searching}
                          onClick={() => openBinding(candidate)}
                        >
                          <Link2 className="size-3.5" />
                          {t("desktop.knowledgeBases.missingSource.bind")}
                        </Button>
                      </div>

                      {bindingId === candidate.candidateId ? (
                        <div className="mt-4 rounded-lg border border-border/70 bg-background p-3">
                          <div className="flex gap-2">
                            <Input
                              value={sourceQuery}
                              disabled={working || searching}
                              onChange={(event) => {
                                setSourceQuery(event.target.value)
                                setHasSearched(false)
                              }}
                              onKeyDown={(event) => {
                                if (event.key === "Enter") void searchSources()
                              }}
                              placeholder={t("desktop.knowledgeBases.missingSource.searchPlaceholder")}
                            />
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={working || searching || !sourceQuery.trim()}
                              onClick={() => void searchSources()}
                            >
                              {searching ? <Loader2 className="size-3.5 animate-spin" /> : <Search className="size-3.5" />}
                              {t("desktop.knowledgeBases.missingSource.search")}
                            </Button>
                          </div>
                          {sourceResults.length ? (
                            <ul className="mt-3 space-y-2">
                              {sourceResults.map((source) => (
                                <li key={source.evidenceId} className="rounded-lg border border-border/60 p-3">
                                  <p className="text-xs font-medium">
                                    {source.documentName}{source.section ? ` · ${source.section}` : ""}
                                  </p>
                                  <p className="mt-1 line-clamp-3 text-xs leading-5 text-muted-foreground">
                                    {source.excerpt}
                                  </p>
                                  <Button
                                    className="mt-2"
                                    size="sm"
                                    disabled={working || searching}
                                    onClick={() => void bindSource(candidate, source)}
                                  >
                                    <Link2 className="size-3.5" />
                                    {t("desktop.knowledgeBases.missingSource.bind")}
                                  </Button>
                                </li>
                              ))}
                            </ul>
                          ) : hasSearched && !searching ? (
                            <p className="mt-3 text-xs text-muted-foreground">
                              {t("desktop.knowledgeBases.missingSource.noSources")}
                            </p>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                  </div>
                </article>
              ))}
            </div>
          </>
        ) : (
          <p className="py-10 text-sm text-muted-foreground">
            {t("desktop.knowledgeBases.missingSource.empty")}
          </p>
        )}
      </div>
    </section>
  )
}
