import { GitCompareArrows, Link2, Loader2, Split } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { useDesktopBridge } from "./bridge-context"
import { nextDesktopRequestId } from "./request-id"
import type {
  DesktopDocumentVersionCandidate,
  DesktopDocumentVersionCandidateDecision,
} from "./contracts"

/** Lets a person decide whether a D3 suggestion belongs to an existing source. */
export function DesktopDocumentVersionCandidatePanel() {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [candidates, setCandidates] = useState<DesktopDocumentVersionCandidate[]>([])
  const [loading, setLoading] = useState(true)
  const [resolvingId, setResolvingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let disposed = false
    void bridge.documentVersionCandidates()
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

  const resolveCandidate = async (
    candidate: DesktopDocumentVersionCandidate,
    decision: DesktopDocumentVersionCandidateDecision,
  ) => {
    setResolvingId(candidate.candidateId)
    setError(null)
    setSaved(false)
    try {
      await bridge.resolveDocumentVersionCandidate(
        candidate.candidateId,
        decision,
        nextDesktopRequestId("document-version"),
      )
      setCandidates((current) => current.filter((item) => item.documentId !== candidate.documentId))
      setSaved(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setResolvingId(null)
    }
  }

  return (
    <section className="mt-8 max-w-4xl" data-testid="desktop-document-version-candidates">
      <div className="rounded-apple-lg border border-border/70 bg-background p-5 shadow-sm md:p-6">
        <div className="flex items-start gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary">
            <GitCompareArrows className="size-4" />
          </div>
          <div>
            <h2 className="font-semibold">{t("desktop.knowledgeBases.versionCandidates.title")}</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              {t("desktop.knowledgeBases.versionCandidates.description")}
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
            {t("desktop.knowledgeBases.versionCandidates.saved")}
          </p>
        ) : null}

        {loading ? (
          <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("desktop.knowledgeBases.versionCandidates.loading")}
          </div>
        ) : candidates.length ? (
          <div className="mt-6 space-y-3">
            {candidates.map((candidate) => {
              const resolving = resolvingId !== null
              const resolvingCandidate = resolvingId === candidate.candidateId
              return (
                <article key={candidate.candidateId} className="rounded-xl border border-border/70 bg-muted/20 p-4">
                  <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] sm:items-center">
                    <div className="min-w-0">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                        {t("desktop.knowledgeBases.versionCandidates.importedDocument")}
                      </p>
                      <p className="mt-1 truncate text-sm font-medium">{candidate.documentName}</p>
                    </div>
                    <GitCompareArrows className="size-4 text-muted-foreground" />
                    <div className="min-w-0">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                        {t("desktop.knowledgeBases.versionCandidates.existingDocument")}
                      </p>
                      <p className="mt-1 truncate text-sm font-medium">{candidate.candidateDocumentName}</p>
                    </div>
                  </div>
                  <p className="mt-3 text-xs text-muted-foreground">
                    {t("desktop.knowledgeBases.versionCandidates.similarity", {
                      lexical: Math.round(candidate.lexicalScore * 100),
                      character: Math.round(candidate.characterScore * 100),
                    })}
                  </p>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      disabled={resolving}
                      onClick={() => void resolveCandidate(candidate, "link_to_candidate")}
                    >
                      {resolvingCandidate ? <Loader2 className="size-3.5 animate-spin" /> : <Link2 className="size-3.5" />}
                      {t("desktop.knowledgeBases.versionCandidates.link")}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={resolving}
                      onClick={() => void resolveCandidate(candidate, "keep_separate")}
                    >
                      <Split className="size-3.5" />
                      {t("desktop.knowledgeBases.versionCandidates.keepSeparate")}
                    </Button>
                  </div>
                </article>
              )
            })}
          </div>
        ) : (
          <p className="py-10 text-sm text-muted-foreground">
            {t("desktop.knowledgeBases.versionCandidates.empty")}
          </p>
        )}
      </div>
    </section>
  )
}
