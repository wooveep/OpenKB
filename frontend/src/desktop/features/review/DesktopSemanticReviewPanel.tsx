import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { useDesktopBridge } from "@/desktop/bridge/context"
import type { SemanticReview, SemanticReviewDecision } from "@/desktop/bridge/contracts/semantic-reviews"
import { createLatestRefresh, type LatestRefresh } from "@/desktop/shared/latest-refresh"
import { nextDesktopRequestId } from "@/desktop/shared/request-id"

/** Human decisions apply to the evidence snapshot displayed on each review card. */
export function DesktopSemanticReviewPanel() {
  const bridge = useDesktopBridge()
  const { t } = useTranslation("common")
  const [items, setItems] = useState<SemanticReview[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const refresh = useRef<LatestRefresh | null>(null)

  useEffect(() => {
    const controller = createLatestRefresh({
      load: () => bridge.semanticReviews(),
      commit: (result) => { setItems(result.items); setError(null) },
      onError: (failure) => setError(failure instanceof Error ? failure.message : String(failure)),
    })
    refresh.current = controller
    controller.request()
    const timer = setInterval(() => controller.request(), 3000)
    return () => { clearInterval(timer); controller.dispose(); refresh.current = null }
  }, [bridge])

  const resolve = async (reviewId: string, decision: SemanticReviewDecision) => {
    setBusy(reviewId)
    try {
      await bridge.resolveSemanticReview(reviewId, decision, nextDesktopRequestId("semantic-review"))
      refresh.current?.request()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    } finally { setBusy(null) }
  }

  return <section className="space-y-3 rounded-lg border border-border/70 p-4">
    <h2 className="font-medium">{t("desktop.semanticReview.title")}</h2>
    <p className="text-sm text-muted-foreground">{t("desktop.semanticReview.description")}</p>
    {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
    {!items.some((item) => item.status === "pending") ? <p className="text-sm text-muted-foreground">{t("desktop.semanticReview.empty")}</p> : null}
    {items.map((item) => <article key={item.reviewId} className="space-y-3 rounded-md border p-3">
      <h3 className="font-medium">{item.candidates.map((candidate) => candidate.title).join(" / ")}</h3>
      <p className="text-sm text-muted-foreground">{t(`desktop.semanticReview.reasons.${item.reason}`)}</p>
      {item.candidates.map((candidate) => <div key={`${candidate.candidateGenerationId}:${candidate.candidateId}`}>
        <p className="text-sm font-medium">{candidate.title}</p>
        <ul className="list-disc space-y-1 pl-5 text-sm">{candidate.claims.map((claim, index) => <li key={index}>
          {claim.text}
          {claim.applicability.length ? <span className="block text-xs text-muted-foreground">{claim.applicability.map(([dimension, value]) => `${dimension}: ${value}`).join("; ")}</span> : null}
        </li>)}</ul>
      </div>)}
      <details><summary className="cursor-pointer text-sm">{t("desktop.semanticReview.evidence")}</summary>
        {item.evidence.map((source) => <blockquote key={source.evidenceId} className="mt-2 whitespace-pre-wrap border-l-2 pl-3 text-sm">{source.text}</blockquote>)}
      </details>
      {item.status === "pending" ? <div className="flex flex-wrap gap-2">
        {item.choices.map((choice) => <Button key={choice} variant="outline" size="sm" disabled={busy !== null}
          onClick={() => void resolve(item.reviewId, choice)}>{t(`desktop.semanticReview.choices.${choice}`)}</Button>)}
      </div> : <p className="text-xs text-muted-foreground">{t("desktop.semanticReview.resolved", { decision: t(`desktop.semanticReview.choices.${item.decision}`), authority: t(`desktop.semanticReview.authorities.${item.authority}`) })}</p>}
    </article>)}
  </section>
}
