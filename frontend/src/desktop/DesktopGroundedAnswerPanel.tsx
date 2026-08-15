import { convertFileSrc } from "@tauri-apps/api/core"
import { Images, Loader2, RotateCcw, SendHorizontal, Square } from "lucide-react"
import { useCallback, useEffect, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { useDesktopBridge } from "./bridge-context"
import type { DesktopAnswerSourceImage, DesktopGroundedAnswer } from "./contracts"
import { nextDesktopRequestId } from "./request-id"
import { formatSourceLocator } from "./source-locator"

type StreamingAnswer = {
  requestId: string
  answerId: string | null
  attempt: number
  content: string
  retrying: boolean
}

/** Ask over the persisted Available Knowledge evidence pack, never browser state. */
export function DesktopGroundedAnswerPanel({
  onOpenOriginal,
}: {
  onOpenOriginal: (documentId: string, locator: Record<string, unknown>) => void
}) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [question, setQuestion] = useState("")
  const [answers, setAnswers] = useState<DesktopGroundedAnswer[]>([])
  const [streaming, setStreaming] = useState<StreamingAnswer | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [answering, setAnswering] = useState(false)

  const refreshAnswers = useCallback(async () => {
    try {
      const result = await bridge.groundedAnswers()
      setAnswers(result.answers)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [bridge])

  useEffect(() => {
    let disposed = false
    let unsubscribe: (() => void) | undefined
    void Promise.resolve().then(refreshAnswers)
    void bridge.subscribe((event) => {
      if (event.kind !== "answer.delta" || disposed) return
      setStreaming((current) => {
        if (current?.requestId !== event.data.requestId) return current
        if (event.data.attempt < current.attempt) return current
        const replace = event.data.replace || event.data.attempt > current.attempt
        return {
          requestId: current.requestId,
          answerId: event.data.answerId,
          attempt: event.data.attempt,
          content: replace ? event.data.delta : current.content + event.data.delta,
          retrying: current.retrying,
        }
      })
    }).then((remove) => {
      if (disposed) remove()
      else unsubscribe = remove
    }).catch(() => undefined)
    return () => {
      disposed = true
      unsubscribe?.()
    }
  }, [bridge, refreshAnswers])

  const ask = async () => {
    const normalized = question.trim()
    if (!normalized || answering) return
    const requestId = nextDesktopRequestId("answer")
    setAnswering(true)
    setError(null)
    setStreaming({ requestId, answerId: null, attempt: 0, content: "", retrying: false })
    try {
      const answer = await bridge.askGrounded(normalized, requestId)
      setAnswers((current) => [
        answer,
        ...current.filter((existing) => existing.answerId !== answer.answerId),
      ])
      setQuestion("")
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setStreaming(null)
      setAnswering(false)
    }
  }

  const retryAnswer = async (answer: DesktopGroundedAnswer) => {
    if (answering || answer.status !== "interrupted") return
    const requestId = nextDesktopRequestId("answer")
    setAnswering(true)
    setError(null)
    setStreaming({ requestId, answerId: answer.answerId, attempt: 0, content: "", retrying: true })
    try {
      const replacement = await bridge.retryInterruptedAnswer(answer.answerId, requestId)
      setAnswers((current) => current.map((item) => (
        item.answerId === replacement.answerId ? replacement : item
      )))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setStreaming(null)
      setAnswering(false)
    }
  }

  const stopAnswer = async () => {
    if (!streaming) return
    try {
      await bridge.cancel(streaming.requestId)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  return (
    <div className="mt-8 space-y-5">
      <section className="rounded-apple-lg border border-border/70 bg-muted/20 p-5 shadow-sm">
        <h2 className="font-semibold">{t("desktop.knowledgeBases.answersTitle")}</h2>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          {t("desktop.knowledgeBases.answersDescription")}
        </p>
        <label className="mt-4 block text-sm font-medium" htmlFor="desktop-grounded-question">
          {t("desktop.knowledgeBases.answerQuestionLabel")}
        </label>
        <textarea
          id="desktop-grounded-question"
          value={question}
          rows={3}
          disabled={answering}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              void ask()
            }
          }}
          placeholder={t("desktop.knowledgeBases.answerQuestionPlaceholder")}
          className="mt-2 w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <div className="mt-3 flex justify-end gap-2">
          {answering ? (
            <Button type="button" variant="outline" onClick={() => void stopAnswer()}>
              <Square className="size-3.5" />
              {t("desktop.knowledgeBases.stopAnswer")}
            </Button>
          ) : null}
          <Button disabled={!question.trim() || answering} onClick={() => void ask()}>
            {answering ? <Loader2 className="size-4 animate-spin" /> : <SendHorizontal className="size-4" />}
            {answering
              ? t("desktop.knowledgeBases.askingQuestion")
              : t("desktop.knowledgeBases.askQuestion")}
          </Button>
        </div>
        {error ? <p className="mt-3 text-sm text-destructive" role="alert">{error}</p> : null}
      </section>

      {streaming ? (
        <AnswerCard
          answerText={streaming.content}
          title={t(
            streaming.retrying
              ? "desktop.knowledgeBases.answerRetrying"
              : "desktop.knowledgeBases.answerStreaming",
          )}
        />
      ) : null}
      {answers.length ? answers.map((answer) => (
        <CompletedAnswerCard
          key={answer.answerId}
          answer={answer}
          onOpenOriginal={onOpenOriginal}
          onRetry={() => void retryAnswer(answer)}
          retrying={answering}
        />
      )) : (
        <p className="rounded-apple-lg border border-dashed border-border/80 p-5 text-sm text-muted-foreground">
          {t("desktop.knowledgeBases.noAnswers")}
        </p>
      )}
    </div>
  )
}

function CompletedAnswerCard({
  answer,
  onOpenOriginal,
  onRetry,
  retrying,
}: {
  answer: DesktopGroundedAnswer
  onOpenOriginal: (documentId: string, locator: Record<string, unknown>) => void
  onRetry: () => void
  retrying: boolean
}) {
  const { t } = useTranslation("common")
  return (
    <AnswerCard answerText={answer.answerText} title={answer.question}>
      {answer.status === "interrupted" ? (
        <section className="mt-4 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
          <p className="font-medium">{t("desktop.knowledgeBases.answerInterrupted")}</p>
          {answer.interruptionReason ? (
            <p className="mt-1 text-muted-foreground">{answer.interruptionReason}</p>
          ) : null}
          <Button
            className="mt-3"
            type="button"
            variant="outline"
            size="sm"
            disabled={retrying}
            onClick={onRetry}
          >
            {retrying ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCcw className="size-3.5" />}
            {retrying
              ? t("desktop.knowledgeBases.answerRetrying")
              : t("desktop.knowledgeBases.retryAnswer")}
          </Button>
        </section>
      ) : null}
      {answer.sourceImages.length ? (
        <AnswerSourceImages
          images={answer.sourceImages}
          citations={answer.citations}
          onOpenOriginal={onOpenOriginal}
        />
      ) : null}
      {answer.citations.length ? (
        <div className="mt-4 border-t border-border/70 pt-4">
          <h3 className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            {t("desktop.knowledgeBases.answerSources")}
          </h3>
          <ol className="mt-2 space-y-2">
            {answer.citations.map((citation, index) => (
              <li key={citation.evidenceId} className="rounded-md bg-muted/60 px-3 py-2 text-sm">
                <button
                  type="button"
                  onClick={() => onOpenOriginal(citation.documentId, citation.locator)}
                  className="block w-full rounded-sm text-left outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <p className="font-medium">
                    [{index + 1}] {citation.documentName} · {citation.section}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {t("desktop.knowledgeBases.answerCitationLocation", {
                      location: formatSourceLocator(citation.locator),
                    })}
                  </p>
                </button>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </AnswerCard>
  )
}

function AnswerSourceImages({
  images,
  citations,
  onOpenOriginal,
}: {
  images: DesktopAnswerSourceImage[]
  citations: DesktopGroundedAnswer["citations"]
  onOpenOriginal: (documentId: string, locator: Record<string, unknown>) => void
}) {
  const { t } = useTranslation("common")
  const [showAll, setShowAll] = useState(false)
  const visibleImages = showAll ? images : images.slice(0, 3)
  return (
    <section className="mt-4 border-t border-border/70 pt-4">
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
        <Images className="size-3.5" />
        {t("desktop.knowledgeBases.answerSourceImages")}
      </h3>
      <div className="mt-2 grid gap-3 sm:grid-cols-3">
        {visibleImages.map((image) => {
          const source = image.filePath ? convertFileSrc(image.filePath) : ""
          const citationIndex = citations.findIndex(
            (citation) => citation.evidenceId === image.evidenceId,
          )
          return source ? (
            <button
              key={image.sourceImageId}
              type="button"
              onClick={() => onOpenOriginal(image.documentId, image.locator)}
              className="overflow-hidden rounded-md border border-border/70 bg-muted/20 text-left outline-none transition-colors hover:border-primary/60 focus-visible:ring-2 focus-visible:ring-ring"
              title={image.altText ?? image.name}
            >
              <img
                src={source}
                alt={image.altText ?? image.name}
                className="h-36 w-full object-contain"
              />
              <span className="block truncate border-t border-border/70 px-2 py-1 text-xs text-muted-foreground">
                {image.altText ?? image.name}
              </span>
              {citationIndex >= 0 ? (
                <span className="block px-2 pb-2 text-[11px] text-muted-foreground">
                  {t("desktop.knowledgeBases.answerImageCitation", { index: citationIndex + 1 })}
                </span>
              ) : null}
            </button>
          ) : null
        })}
      </div>
      {images.length > 3 ? (
        <Button
          className="mt-3"
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setShowAll((current) => !current)}
        >
          {showAll
            ? t("desktop.knowledgeBases.answerShowFewerSourceImages")
            : t("desktop.knowledgeBases.answerViewAllSourceImages", { count: images.length })}
        </Button>
      ) : null}
    </section>
  )
}

function AnswerCard({
  answerText,
  title,
  children,
}: {
  answerText: string
  title: string
  children?: ReactNode
}) {
  return (
    <article className="rounded-apple-lg border border-border/70 bg-background p-5 shadow-sm">
      <h2 className="font-semibold">{title}</h2>
      <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-foreground">{answerText}</p>
      {children}
    </article>
  )
}
