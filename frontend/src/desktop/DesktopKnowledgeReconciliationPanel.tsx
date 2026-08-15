import { GitPullRequest, Loader2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { useDesktopBridge } from "./bridge-context"
import type { DesktopKnowledgeReconciliationConflict } from "./contracts"

/** Shows isolated Concept/Entity changes; decisions are intentionally added by T23. */
export function DesktopKnowledgeReconciliationPanel() {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [conflicts, setConflicts] = useState<DesktopKnowledgeReconciliationConflict[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let disposed = false
    void bridge.knowledgeReconciliationConflicts()
      .then((result) => {
        if (disposed) return
        setConflicts(result.conflicts)
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

  return (
    <section className="mt-8 max-w-4xl" data-testid="desktop-knowledge-reconciliation-conflicts">
      <div className="rounded-apple-lg border border-border/70 bg-background p-5 shadow-sm md:p-6">
        <div className="flex items-start gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-amber-500/10 text-amber-700 dark:text-amber-300">
            <GitPullRequest className="size-4" />
          </div>
          <div>
            <h2 className="font-semibold">{t("desktop.knowledgeBases.reconciliation.title")}</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              {t("desktop.knowledgeBases.reconciliation.description")}
            </p>
          </div>
        </div>

        {error ? (
          <p className="mt-5 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : null}
        {loading ? (
          <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("desktop.knowledgeBases.reconciliation.loading")}
          </div>
        ) : conflicts.length ? (
          <div className="mt-6 space-y-3">
            {conflicts.map((conflict) => (
              <article key={conflict.candidateId} className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <p className="font-medium">{conflict.title}</p>
                  <span className="rounded-full bg-background px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                    {conflict.kind === "entity"
                      ? t("desktop.knowledgeBases.reconciliation.entity")
                      : t("desktop.knowledgeBases.reconciliation.concept")}
                  </span>
                </div>
                <p className="mt-1 text-sm text-muted-foreground">
                  {t("desktop.knowledgeBases.reconciliation.fromDocument", {
                    document: conflict.documentName,
                  })}
                </p>
                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <ConflictExcerpt
                    label={t("desktop.knowledgeBases.reconciliation.incoming")}
                    content={conflict.contentMarkdown}
                  />
                  <ConflictExcerpt
                    label={conflict.baselineKind === "user_revision"
                      ? t("desktop.knowledgeBases.reconciliation.userRevision")
                      : t("desktop.knowledgeBases.reconciliation.publishedKnowledge")}
                    content={conflict.baselineContentMarkdown}
                  />
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="py-10 text-sm text-muted-foreground">
            {t("desktop.knowledgeBases.reconciliation.empty")}
          </p>
        )}
      </div>
    </section>
  )
}

function ConflictExcerpt({ label, content }: { label: string; content: string }) {
  return (
    <div className="rounded-lg border border-border/70 bg-muted/20 p-3">
      <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
      <p className="mt-2 line-clamp-4 whitespace-pre-wrap text-sm leading-6">{content}</p>
    </div>
  )
}
