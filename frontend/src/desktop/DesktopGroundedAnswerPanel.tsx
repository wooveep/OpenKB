import { Loader2, SendHorizontal } from "lucide-react"
import { useCallback, useEffect, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { useDesktopBridge } from "./bridge-context"
import type { DesktopGroundedAnswer } from "./contracts"

let requestSequence = 0

function nextAnswerRequestId(): string {
  requestSequence += 1
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `desktop-answer-${Date.now()}-${requestSequence}`
}

type StreamingAnswer = {
  requestId: string
  answerId: string | null
  attempt: number
  content: string
}

/** Ask over the persisted Available Knowledge evidence pack, never browser state. */
export function DesktopGroundedAnswerPanel() {
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
    const requestId = nextAnswerRequestId()
    setAnswering(true)
    setError(null)
    setStreaming({ requestId, answerId: null, attempt: 0, content: "" })
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
        <div className="mt-3 flex justify-end">
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
          title={t("desktop.knowledgeBases.answerStreaming")}
        />
      ) : null}
      {answers.length ? answers.map((answer) => <CompletedAnswerCard key={answer.answerId} answer={answer} />) : (
        <p className="rounded-apple-lg border border-dashed border-border/80 p-5 text-sm text-muted-foreground">
          {t("desktop.knowledgeBases.noAnswers")}
        </p>
      )}
    </div>
  )
}

function CompletedAnswerCard({ answer }: { answer: DesktopGroundedAnswer }) {
  const { t } = useTranslation("common")
  return (
    <AnswerCard answerText={answer.answerText} title={answer.question}>
      {answer.citations.length ? (
        <div className="mt-4 border-t border-border/70 pt-4">
          <h3 className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            {t("desktop.knowledgeBases.answerSources")}
          </h3>
          <ol className="mt-2 space-y-2">
            {answer.citations.map((citation, index) => (
              <li key={citation.evidenceId} className="rounded-md bg-muted/60 px-3 py-2 text-sm">
                <p className="font-medium">
                  [{index + 1}] {citation.documentName} · {citation.section}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t("desktop.knowledgeBases.answerCitationLocation", {
                    location: formatLocator(citation.locator),
                  })}
                </p>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </AnswerCard>
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

function formatLocator(locator: Record<string, unknown>): string {
  const values = ["page", "slide", "sheet", "cell", "ordinal"]
    .flatMap((key) => locator[key] === undefined ? [] : [`${key}: ${String(locator[key])}`])
  return values.length ? values.join(" · ") : "document"
}
