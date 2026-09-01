import { convertFileSrc } from "@tauri-apps/api/core"
import {
  ChevronLeft,
  ChevronRight,
  Images,
  Loader2,
  MessageSquarePlus,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  RotateCcw,
  Search,
  SendHorizontal,
  Square,
  Trash2,
} from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { openKBEvidenceOrdinals } from "@/components/markdown-evidence"
import MarkdownView from "@/components/MarkdownView"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import { useDesktopBridge } from "./bridge-context"
import { DesktopCapabilityDegradationNotice } from "./DesktopCapabilityDegradationNotice"
import {
  DesktopLiveModelActivityDetails,
  type DesktopLiveModelActivity,
} from "./DesktopModelActivityDetails"
import type {
  DesktopAnswerVersion,
  DesktopConversation,
  DesktopConversationSummary,
} from "./contracts"
import { nextDesktopRequestId } from "./request-id"
import { formatSourceLocator } from "./source-locator"

type StreamState = {
  requestId: string
  conversationId: string
  messageId: string | null
  question: string | null
  content: string
  attempt: number
  activity: DesktopLiveModelActivity | null
}

/** Codex-style multi-conversation workspace backed by SQLite Conversation objects. */
export function DesktopConversationPanel({
  requestedConversationId,
  requestedMessageId,
  requestKey = 0,
  onOpenOriginal,
  onOpenModelSettings,
}: {
  requestedConversationId?: string | null
  requestedMessageId?: string | null
  requestKey?: number
  onOpenOriginal: (documentId: string, locator: Record<string, unknown>) => void
  onOpenModelSettings: () => void
}) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [summaries, setSummaries] = useState<DesktopConversationSummary[]>([])
  const [conversation, setConversation] = useState<DesktopConversation | null>(null)
  const [search, setSearch] = useState("")
  const [listCollapsed, setListCollapsed] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [streams, setStreams] = useState<Record<string, StreamState>>({})
  const [evidence, setEvidence] = useState<DesktopAnswerVersion | null>(null)
  const [evidenceTab, setEvidenceTab] = useState<"sources" | "images">("sources")
  const [evidenceFocusIndex, setEvidenceFocusIndex] = useState<number | null>(null)
  const selectedConversationId = useRef<string | null>(null)
  const selectionRead = useRef(0)
  const conversationRef = useRef<DesktopConversation | null>(null)
  const pendingDraft = useRef<{ conversationId: string; text: string } | null>(null)
  const draftSaveTimer = useRef<number | null>(null)
  const draftSaveChain = useRef<Promise<void>>(Promise.resolve())
  const boot = useRef<Promise<void> | null>(null)
  useEffect(() => {
    conversationRef.current = conversation
  }, [conversation])

  const refreshList = useCallback(async (query = search) => {
    const result = await bridge.conversations(query)
    setSummaries(result.conversations)
    return result
  }, [bridge, search])

  const persistDraft = useCallback((conversationId: string, text: string): Promise<void> => {
    const operation = draftSaveChain.current
      .catch(() => undefined)
      .then(() => bridge.saveConversationDraft(
        conversationId,
        text,
        nextDesktopRequestId("conversation-draft"),
      ))
      .then(() => undefined)
    draftSaveChain.current = operation.catch(() => undefined)
    return operation
  }, [bridge])

  const flushDraft = useCallback((): Promise<void> => {
    if (draftSaveTimer.current !== null) {
      window.clearTimeout(draftSaveTimer.current)
      draftSaveTimer.current = null
    }
    const pending = pendingDraft.current
    pendingDraft.current = null
    return pending ? persistDraft(pending.conversationId, pending.text) : draftSaveChain.current
  }, [persistDraft])

  const queueDraft = useCallback((conversationId: string, text: string) => {
    pendingDraft.current = { conversationId, text }
    if (draftSaveTimer.current !== null) window.clearTimeout(draftSaveTimer.current)
    draftSaveTimer.current = window.setTimeout(() => {
      void flushDraft().catch(() => undefined)
    }, 250)
  }, [flushDraft])

  useEffect(() => () => {
    void flushDraft().catch(() => undefined)
  }, [flushDraft])

  const selectConversation = useCallback(async (conversationId: string) => {
    const read = selectionRead.current + 1
    selectionRead.current = read
    selectedConversationId.current = conversationId
    setError(null)
    try {
      const current = conversationRef.current
      if (current && current.conversationId !== conversationId) {
        pendingDraft.current = { conversationId: current.conversationId, text: current.draftText }
        await flushDraft()
      }
      const selected = await bridge.getConversation(conversationId)
      if (read === selectionRead.current && selectedConversationId.current === conversationId) {
        conversationRef.current = selected
        setConversation(selected)
      }
    } catch (cause) {
      if (read === selectionRead.current) setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [bridge, flushDraft])

  useEffect(() => {
    if (boot.current === null) {
      boot.current = (async () => {
        const result = await bridge.conversations()
        setSummaries(result.conversations)
        if (result.conversations.length) {
          const id = result.lastConversationId ?? result.conversations[0].conversationId
          selectedConversationId.current = id
          setConversation(await bridge.getConversation(id))
        } else {
          const created = await bridge.createConversation(undefined, nextDesktopRequestId("conversation"))
          selectedConversationId.current = created.conversationId
          setConversation(created)
          await refreshList("")
        }
      })().catch((cause) => {
        setError(cause instanceof Error ? cause.message : String(cause))
      }).finally(() => setLoading(false))
    }
  }, [bridge, refreshList])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refreshList(search).catch(() => undefined)
    }, 180)
    return () => window.clearTimeout(timer)
  }, [refreshList, search])

  useEffect(() => {
    let disposed = false
    let unsubscribe: (() => void) | undefined
    void bridge.subscribe((event) => {
      if (disposed) return
      if (event.kind === "model.call_lifecycle") {
        setStreams((current) => {
          const stream = Object.values(current).find(
            (item) => item.requestId === event.data.requestId,
          )
          return stream ? {
            ...current,
            [stream.conversationId]: {
              ...stream,
              activity: { ...event.data, observedAtMs: Date.now() },
            },
          } : current
        })
        return
      }
      if (event.kind !== "answer.delta") return
      setStreams((current) => {
        const stream = Object.values(current).find((item) => item.requestId === event.data.requestId)
        if (!stream || event.data.attempt < stream.attempt) return current
        const replace = event.data.replace || event.data.attempt > stream.attempt
        return {
          ...current,
          [stream.conversationId]: {
            ...stream,
            messageId: event.data.answerId,
            attempt: event.data.attempt,
            content: replace ? event.data.delta : stream.content + event.data.delta,
            activity: stream.activity,
          },
        }
      })
    }).then((remove) => {
      if (disposed) remove()
      else unsubscribe = remove
    }).catch(() => undefined)
    return () => { disposed = true; unsubscribe?.() }
  }, [bridge])

  useEffect(() => {
    if (!requestedConversationId) return
    let disposed = false
    void (async () => {
      await boot.current
      if (!disposed && selectedConversationId.current !== requestedConversationId) {
        await selectConversation(requestedConversationId)
      }
    })()
    return () => { disposed = true }
  }, [requestKey, requestedConversationId, selectConversation])

  useEffect(() => {
    if (!requestedMessageId || conversation?.conversationId !== requestedConversationId) return
    const frame = window.requestAnimationFrame(() => {
      const message = document.getElementById(`conversation-message-${requestedMessageId}`)
      message?.scrollIntoView({ block: "center", behavior: "smooth" })
      message?.focus({ preventScroll: true })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [conversation?.conversationId, requestKey, requestedConversationId, requestedMessageId])

  const createConversation = async () => {
    try {
      const current = conversationRef.current
      if (current) {
        pendingDraft.current = { conversationId: current.conversationId, text: current.draftText }
        await flushDraft()
      }
      const created = await bridge.createConversation(undefined, nextDesktopRequestId("conversation"))
      await refreshList("")
      selectionRead.current += 1
      selectedConversationId.current = created.conversationId
      conversationRef.current = created
      setConversation(created)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const renameConversation = async () => {
    if (!conversation) return
    const title = window.prompt(t("desktop.conversations.renamePrompt"), conversation.title)?.trim()
    if (!title) return
    try {
      const updated = await bridge.renameConversation(
        conversation.conversationId,
        title,
        nextDesktopRequestId("conversation"),
      )
      setConversation(updated)
      await refreshList("")
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const deleteConversation = async () => {
    if (!conversation || !window.confirm(t("desktop.conversations.deleteConfirm"))) return
    try {
      const result = await bridge.deleteConversation(
        conversation.conversationId,
        nextDesktopRequestId("conversation"),
      )
      setSummaries(result.conversations)
      selectionRead.current += 1
      selectedConversationId.current = null
      conversationRef.current = null
      setConversation(null)
      if (result.conversations.length) {
        await selectConversation(result.lastConversationId ?? result.conversations[0].conversationId)
      } else {
        await createConversation()
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const ask = async () => {
    if (!conversation) return
    const question = conversation.draftText.trim()
    if (!question || streams[conversation.conversationId]) return
    pendingDraft.current = { conversationId: conversation.conversationId, text: conversation.draftText }
    try {
      await flushDraft()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      return
    }
    const conversationId = conversation.conversationId
    const requestId = nextDesktopRequestId("conversation-answer")
    const cleared = { ...conversation, draftText: "" }
    conversationRef.current = cleared
    setConversation(cleared)
    setStreams((current) => ({
      ...current,
      [conversationId]: {
        requestId,
        conversationId,
        messageId: null,
        question,
        content: "",
        attempt: 0,
        activity: null,
      },
    }))
    setSummaries((current) => current.map((item) => item.conversationId === conversationId ? { ...item, generating: true } : item))
    setError(null)
    setNotice(null)
    try {
      const updated = await bridge.askConversation(conversationId, question, requestId)
      if (selectedConversationId.current === conversationId) {
        conversationRef.current = updated
        setConversation(updated)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      if (selectedConversationId.current === conversationId) {
        void selectConversation(conversationId)
      }
    } finally {
      setStreams((current) => {
        const next = { ...current }
        delete next[conversationId]
        return next
      })
      void refreshList("")
    }
  }

  const updateDraft = (value: string) => {
    const current = conversationRef.current
    if (!current) return
    const updated = { ...current, draftText: value }
    conversationRef.current = updated
    setConversation(updated)
    queueDraft(current.conversationId, value)
  }

  const regenerate = async (messageId: string) => {
    if (!conversation || streams[conversation.conversationId]) return
    const conversationId = conversation.conversationId
    const requestId = nextDesktopRequestId("conversation-regenerate")
    setError(null)
    setNotice(null)
    setStreams((current) => ({
      ...current,
      [conversationId]: {
        requestId,
        conversationId,
        messageId,
        question: null,
        content: "",
        attempt: 0,
        activity: null,
      },
    }))
    try {
      const updated = await bridge.regenerateConversationAnswer(conversationId, messageId, requestId)
      if (selectedConversationId.current === conversationId) {
        conversationRef.current = updated
        setConversation(updated)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setStreams((current) => {
        const next = { ...current }
        delete next[conversationId]
        return next
      })
      void refreshList("")
    }
  }

  const selectVersion = async (messageId: string, answerVersionId: string) => {
    if (!conversation) return
    try {
      const updated = await bridge.selectAnswerVersion(
        conversation.conversationId,
        messageId,
        answerVersionId,
        nextDesktopRequestId("answer-version"),
      )
      conversationRef.current = updated
      setConversation(updated)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const currentStream = conversation ? streams[conversation.conversationId] : undefined

  const stopCurrentStream = async () => {
    if (!currentStream) return
    try {
      const result = await bridge.cancel(currentStream.requestId)
      if (result.cancelled) {
        setNotice(t("desktop.knowledgeBases.answerCancellationWarning"))
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  return (
    <div className="-m-4 flex h-[calc(100vh-3.5rem)] min-h-[34rem] md:-m-6" data-testid="desktop-conversations">
      <aside className={cn("shrink-0 border-r border-border/70 bg-muted/15 transition-[width] duration-150 motion-reduce:transition-none", listCollapsed ? "w-12" : "w-64")}>
        <div className="flex items-center justify-between gap-2 p-2">
          {!listCollapsed ? <Button size="sm" className="min-w-0 flex-1" onClick={() => void createConversation()}><MessageSquarePlus className="size-4" />{t("desktop.conversations.new")}</Button> : null}
          <Button size="icon" variant="ghost" className="size-8" onClick={() => setListCollapsed((current) => !current)} aria-label={t(listCollapsed ? "desktop.conversations.expand" : "desktop.conversations.collapse")}>
            {listCollapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
          </Button>
        </div>
        {!listCollapsed ? (
          <>
            <label className="relative mx-2 block"><Search className="absolute left-2.5 top-2.5 size-3.5 text-muted-foreground" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t("desktop.conversations.search")} className="h-8 w-full rounded-md border border-input bg-background pl-8 pr-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring" /></label>
            <ConversationList items={summaries} selectedId={conversation?.conversationId ?? null} onSelect={(id) => void selectConversation(id)} />
          </>
        ) : null}
      </aside>

      <main className="relative flex min-w-0 flex-1 flex-col bg-background">
        {loading ? <div className="grid flex-1 place-items-center text-sm text-muted-foreground"><span className="flex items-center gap-2"><Loader2 className="size-4 animate-spin" />{t("desktop.conversations.loading")}</span></div> : conversation ? (
          <>
            <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-border/70 px-4">
              <h1 className="truncate text-sm font-semibold">{conversation.title}</h1>
              <div className="flex gap-1"><Button size="icon" variant="ghost" className="size-8" onClick={() => void renameConversation()} aria-label={t("desktop.conversations.rename")}><Pencil className="size-3.5" /></Button><Button size="icon" variant="ghost" className="size-8 text-destructive" disabled={Boolean(currentStream) || conversation.messages.some((message) => message.status === "generating")} onClick={() => void deleteConversation()} aria-label={t("desktop.conversations.delete")}><Trash2 className="size-3.5" /></Button></div>
            </header>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-36 pt-8">
              <div className="mx-auto max-w-[840px] space-y-8">
                {conversation.messages.map((message) => message.role === "user" ? (
                  <div id={`conversation-message-${message.messageId}`} tabIndex={-1} key={message.messageId} className="flex scroll-m-24 justify-end outline-none focus-visible:ring-2 focus-visible:ring-ring"><div className="max-w-[78%] rounded-2xl rounded-br-md bg-muted px-4 py-2.5 text-sm leading-6">{message.content}</div></div>
                ) : (
                  <div id={`conversation-message-${message.messageId}`} tabIndex={-1} key={message.messageId} className="scroll-m-24 outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    <AssistantMessage
                      message={message}
                      stream={currentStream?.messageId === message.messageId ? currentStream : undefined}
                      onRegenerate={() => void regenerate(message.messageId)}
                      onOpenModelSettings={onOpenModelSettings}
                      onSelectVersion={(versionId) => void selectVersion(message.messageId, versionId)}
                      onOpenEvidence={(version, tab, focusIndex = null) => { setEvidence(version); setEvidenceTab(tab); setEvidenceFocusIndex(focusIndex) }}
                    />
                  </div>
                ))}
                {currentStream?.question ? <PendingTurn stream={currentStream} /> : null}
                {!conversation.messages.length && !currentStream ? <p className="py-24 text-center text-sm text-muted-foreground">{t("desktop.conversations.empty")}</p> : null}
              </div>
            </div>
            <Composer
              value={conversation.draftText}
              generating={Boolean(currentStream)}
              onChange={updateDraft}
              onSend={() => void ask()}
              onStop={() => void stopCurrentStream()}
            />
          </>
        ) : null}
        {error ? <p className="absolute bottom-28 left-1/2 z-10 w-[min(42rem,calc(100%-2rem))] -translate-x-1/2 rounded-lg border border-destructive/30 bg-background px-3 py-2 text-sm text-destructive shadow-lg" role="alert">{error}</p> : null}
        {notice ? <p className="absolute bottom-28 left-1/2 z-10 w-[min(42rem,calc(100%-2rem))] -translate-x-1/2 rounded-lg border border-amber-500/40 bg-background px-3 py-2 text-sm text-amber-800 shadow-lg dark:text-amber-200" role="status">{notice}</p> : null}
      </main>
        <EvidenceDrawer version={evidence} tab={evidenceTab} focusIndex={evidenceFocusIndex} onTabChange={setEvidenceTab} onClose={() => { setEvidence(null); setEvidenceFocusIndex(null) }} onOpenOriginal={onOpenOriginal} />
    </div>
  )
}

function ConversationList({ items, selectedId, onSelect }: { items: DesktopConversationSummary[]; selectedId: string | null; onSelect: (id: string) => void }) {
  const { t } = useTranslation("common")
  const groups = groupConversations(items)
  return <div className="mt-3 h-[calc(100vh-8.5rem)] overflow-y-auto px-2">{groups.map((group) => group.items.length ? <section key={group.key} className="mb-4"><h2 className="px-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">{t(`desktop.conversations.groups.${group.key}`)}</h2><div className="mt-1 space-y-0.5">{group.items.map((item) => <button key={item.conversationId} type="button" onClick={() => onSelect(item.conversationId)} aria-current={selectedId === item.conversationId ? "page" : undefined} className={cn("flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring", selectedId === item.conversationId ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-accent/70 hover:text-accent-foreground")}><span className="min-w-0 flex-1 truncate">{item.title}</span>{item.generating ? <Loader2 className="size-3 animate-spin" /> : null}</button>)}</div></section> : null)}</div>
}

function AssistantMessage({ message, stream, onRegenerate, onOpenModelSettings, onSelectVersion, onOpenEvidence }: { message: DesktopConversation["messages"][number]; stream?: StreamState; onRegenerate: () => void; onOpenModelSettings: () => void; onSelectVersion: (id: string) => void; onOpenEvidence: (version: DesktopAnswerVersion, tab: "sources" | "images", focusIndex?: number | null) => void }) {
  const { t } = useTranslation("common")
  const selected = message.answerVersions.find((version) => version.answerVersionId === message.selectedAnswerVersionId) ?? message.answerVersions.at(-1)
  const text = stream ? stream.content : selected?.answerText ?? ""
  const evidenceCount = stream ? 0 : selected?.citations.length ?? 0
  const citedEvidenceIds = new Set(validEvidenceOrdinals(text, evidenceCount).map((ordinal) => selected?.citations[ordinal - 1]?.evidenceId).filter(Boolean))
  const inlineImages = stream ? [] : selected?.sourceImages.filter((image) => citedEvidenceIds.has(image.evidenceId) && image.sourceAvailable && image.filePath).slice(0, 3) ?? []
  useEffect(() => {
    if (!selected || stream) return
    for (const ordinal of invalidEvidenceOrdinals(text, selected.citations.length)) {
      console.warn("answer_evidence_marker_invalid", { messageId: message.messageId, ordinal, evidenceCount: selected.citations.length })
    }
  }, [message.messageId, selected, stream, text])
  return <article className="w-full"><div className="text-sm leading-7"><MarkdownView source={text} finalized={!stream} evidenceCount={evidenceCount} onEvidenceRef={stream ? undefined : (ordinal) => { if (selected) onOpenEvidence(selected, "sources", ordinal - 1) }} /></div>{selected && !stream ? <DesktopCapabilityDegradationNotice codes={selected.degradations} onOpenModelSettings={onOpenModelSettings} onRetry={onRegenerate} /> : null}{stream?.activity ? <div className="mt-3 text-xs"><DesktopLiveModelActivityDetails activity={stream.activity} /></div> : null}{message.status === "interrupted" ? <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">{selected?.interruptionReason ?? t("desktop.conversations.interrupted")}</p> : null}{inlineImages.length ? <div className="mt-4 grid grid-cols-3 gap-2">{inlineImages.map((image) => <button key={image.sourceImageId} type="button" onClick={() => onOpenEvidence(selected!, "images")} className="overflow-hidden rounded-lg border border-border/70 bg-muted/20"><img src={convertFileSrc(image.filePath)} alt={image.altText ?? image.name} className="h-28 w-full object-contain" /></button>)}</div> : null}<div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">{selected && !stream ? <Button size="sm" variant="outline" className="h-7" onClick={() => onOpenEvidence(selected, "sources")}>{t("desktop.conversations.evidence", { citations: selected.citations.length, images: selected.sourceImages.length })}</Button> : null}<Button size="icon" variant="ghost" className="size-7" disabled={Boolean(stream)} onClick={onRegenerate} aria-label={t("desktop.conversations.regenerate")}>{stream ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCcw className="size-3.5" />}</Button>{message.answerVersions.length > 1 && selected && !stream ? <div className="flex items-center gap-1"><Button size="icon" variant="ghost" className="size-7" disabled={selected.versionNumber <= 1} onClick={() => onSelectVersion(message.answerVersions[selected.versionNumber - 2].answerVersionId)}><ChevronLeft className="size-3.5" /></Button><span>{selected.versionNumber}/{message.answerVersions.length}</span><Button size="icon" variant="ghost" className="size-7" disabled={selected.versionNumber >= message.answerVersions.length} onClick={() => onSelectVersion(message.answerVersions[selected.versionNumber].answerVersionId)}><ChevronRight className="size-3.5" /></Button></div> : null}</div></article>
}

function PendingTurn({ stream }: { stream: StreamState }) {
  const { t } = useTranslation("common")
  return <><div className="flex justify-end"><div className="max-w-[78%] rounded-2xl rounded-br-md bg-muted px-4 py-2.5 text-sm leading-6">{stream.question}</div></div><article><p className="mb-2 flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3 animate-spin" />{t("desktop.conversations.generating")}</p>{stream.activity ? <div className="mb-3 text-xs"><DesktopLiveModelActivityDetails activity={stream.activity} /></div> : null}<MarkdownView source={stream.content} finalized={false} /></article></>
}

function Composer({ value, generating, onChange, onSend, onStop }: { value: string; generating: boolean; onChange: (value: string) => void; onSend: () => void; onStop: () => void }) {
  const { t } = useTranslation("common")
  const composing = useRef(false)
  return <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-background via-background to-transparent px-4 pb-4 pt-8"><div className="mx-auto flex max-w-[840px] items-end gap-2 rounded-2xl border border-border bg-background p-2 shadow-lg"><textarea value={value} rows={2} onChange={(event) => onChange(event.target.value)} onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }} onKeyDown={(event) => { if (event.key !== "Enter" || composing.current) return; if (event.shiftKey) return; event.preventDefault(); onSend() }} placeholder={t("desktop.conversations.placeholder")} className="max-h-36 min-h-12 flex-1 resize-none bg-transparent px-2 py-2 text-sm outline-none" />{generating ? <Button size="icon" variant="outline" onClick={onStop} aria-label={t("desktop.knowledgeBases.stopAnswer")}><Square className="size-4" /></Button> : <Button size="icon" disabled={!value.trim()} onClick={onSend} aria-label={t("desktop.knowledgeBases.askQuestion")}><SendHorizontal className="size-4" /></Button>}</div></div>
}

function EvidenceDrawer({ version, tab, focusIndex, onTabChange, onClose, onOpenOriginal }: { version: DesktopAnswerVersion | null; tab: "sources" | "images"; focusIndex: number | null; onTabChange: (tab: "sources" | "images") => void; onClose: () => void; onOpenOriginal: (documentId: string, locator: Record<string, unknown>) => void }) {
  const { t } = useTranslation("common")
  useEffect(() => {
    if (!version || tab !== "sources" || focusIndex === null) return
    const frame = window.requestAnimationFrame(() => document.getElementById(`evidence-${version.answerVersionId}-${focusIndex}`)?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [focusIndex, tab, version])
  return <Sheet open={version !== null} onOpenChange={(open) => { if (!open) onClose() }}><SheetContent className="w-full overflow-y-auto sm:max-w-[420px]"><SheetHeader><SheetTitle>{t("desktop.conversations.evidenceTitle")}</SheetTitle><SheetDescription>{t("desktop.conversations.evidenceDescription")}</SheetDescription></SheetHeader>{version ? <><Tabs value={tab} onValueChange={(value) => onTabChange(value as "sources" | "images")} className="mt-5"><TabsList className="w-full"><TabsTrigger value="sources" className="flex-1">{t("desktop.conversations.sources", { count: version.citations.length })}</TabsTrigger><TabsTrigger value="images" className="flex-1">{t("desktop.conversations.images", { count: version.sourceImages.length })}</TabsTrigger></TabsList><TabsContent value="sources" className="space-y-2">{version.citations.map((citation, index) => <button id={`evidence-${version.answerVersionId}-${index}`} key={citation.evidenceId} type="button" disabled={!citation.sourceAvailable} onClick={() => onOpenOriginal(citation.documentId, citation.locator)} className="w-full rounded-lg border border-border/70 p-3 text-left text-sm outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"><strong>[{index + 1}] {citation.documentName} · {citation.section}</strong><span className="mt-1 block text-xs text-muted-foreground">{formatSourceLocator(citation.locator)}</span><span className="mt-2 line-clamp-4 block text-xs leading-5 text-muted-foreground">{citation.excerpt}</span>{!citation.sourceAvailable ? <span className="mt-2 block text-xs text-amber-700">{t("desktop.conversations.sourceUnavailable")}</span> : null}</button>)}</TabsContent><TabsContent value="images" className="grid gap-3 sm:grid-cols-2">{version.sourceImages.map((image) => image.sourceAvailable && image.filePath ? <button key={image.sourceImageId} type="button" onClick={() => onOpenOriginal(image.documentId, image.locator)} className="overflow-hidden rounded-lg border border-border/70 text-left"><img src={convertFileSrc(image.filePath)} alt={image.altText ?? image.name} className="h-40 w-full object-contain" /><span className="block truncate border-t px-2 py-1.5 text-xs">{image.altText ?? image.name}</span></button> : <div key={image.sourceImageId} className="rounded-lg border border-border/70 p-3 text-xs text-muted-foreground"><Images className="mb-2 size-5" />{t("desktop.conversations.sourceUnavailable")}</div>)}</TabsContent></Tabs><NavigationTraceDetails trace={version.retrievalTrace} /></> : null}</SheetContent></Sheet>
}

function NavigationTraceDetails({ trace }: { trace: DesktopAnswerVersion["retrievalTrace"] }) {
  const { t } = useTranslation("common")
  const visible = trace.navigationSnapshotIds.length > 0
    || trace.navigationReadCount > 0
    || trace.navigationRoundCount > 0
    || trace.groundingInputBudgetTokens > 0
  if (!visible) return null
  return (
    <details className="mt-4 rounded-lg border border-border/70 bg-muted/20 p-3 text-xs">
      <summary className="cursor-pointer font-medium">{t("desktop.conversations.navigationTrace.title")}</summary>
      <dl className="mt-3 grid grid-cols-[1fr_auto] gap-x-3 gap-y-2 text-muted-foreground">
        <dt>{t("desktop.conversations.navigationTrace.coverage")}</dt>
        <dd>{t(`desktop.conversations.navigationTrace.coverageStates.${trace.coverageGateState || "not_applicable"}`)}</dd>
        <dt>{t("desktop.conversations.navigationTrace.rounds")}</dt><dd>{trace.navigationRoundCount}</dd>
        <dt>{t("desktop.conversations.navigationTrace.stopReason")}</dt>
        <dd>{t(`desktop.conversations.navigationTrace.stopReasons.${trace.navigationStopReason || "legacy"}`)}</dd>
        <dt>{t("desktop.conversations.navigationTrace.modelCalls")}</dt><dd>{trace.navigationModelCalls}</dd>
        <dt>{t("desktop.conversations.navigationTrace.reads")}</dt><dd>{trace.navigationReadCount}</dd>
        <dt>{t("desktop.conversations.navigationTrace.logicalReads")}</dt><dd>{trace.navigationLogicalReadCount}</dd>
        <dt>{t("desktop.conversations.navigationTrace.sourceWindows")}</dt><dd>{trace.sourceWindowCount}</dd>
        <dt>{t("desktop.conversations.navigationTrace.sourceTokens")}</dt><dd>{trace.navigationSourceTokens}</dd>
        <dt>{t("desktop.conversations.navigationTrace.linkHops")}</dt><dd>{trace.linkHopCount}</dd>
        <dt>{t("desktop.conversations.navigationTrace.pageTreeSupplements")}</dt><dd>{trace.pageTreeSupplementCount}</dd>
        <dt>{t("desktop.conversations.navigationTrace.budget")}</dt><dd>{trace.evidenceInputTokens} + {trace.guidanceInputTokens} / {trace.groundingInputBudgetTokens}</dd>
      </dl>
      {trace.coverageAspects.length ? (
        <div className="mt-3 border-t border-border/60 pt-3">
          <p className="font-medium">{t("desktop.conversations.navigationTrace.aspects")}</p>
          <ul className="mt-1 space-y-1 text-muted-foreground">
            {trace.coverageAspects.map((item) => (
              <li key={item.aspect}>
                {item.aspect} · {t(`desktop.conversations.navigationTrace.aspectStates.${item.status}`)} · {t("desktop.conversations.navigationTrace.evidenceCount", { count: item.evidenceIds.length })}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {trace.navigationRoutes.length ? <div className="mt-3 border-t border-border/60 pt-3"><p className="font-medium">{t("desktop.conversations.navigationTrace.routes")}</p><ul className="mt-1 space-y-1 text-muted-foreground">{trace.navigationRoutes.map((route) => <li key={route} className="break-all">{route}</li>)}</ul></div> : null}
    </details>
  )
}

function validEvidenceOrdinals(text: string, evidenceCount: number): number[] {
  return openKBEvidenceOrdinals(text).filter((ordinal) => ordinal >= 1 && ordinal <= evidenceCount)
}

function invalidEvidenceOrdinals(text: string, evidenceCount: number): number[] {
  return [...new Set(openKBEvidenceOrdinals(text).filter((ordinal) => ordinal < 1 || ordinal > evidenceCount))]
}

function groupConversations(items: DesktopConversationSummary[]) {
  const now = Date.now()
  const day = 86_400_000
  return [
    { key: "today", items: items.filter((item) => now - Date.parse(item.updatedAt) < day) },
    { key: "week", items: items.filter((item) => { const age = now - Date.parse(item.updatedAt); return age >= day && age < 7 * day }) },
    { key: "older", items: items.filter((item) => now - Date.parse(item.updatedAt) >= 7 * day) },
  ]
}
