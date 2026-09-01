import { Check, GitPullRequest, Loader2, RotateCcw } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { useDesktopBridge } from "./bridge-context"
import { nextDesktopRequestId } from "./request-id"
import type {
  DesktopKnowledgeReconciliationConflict,
  DesktopKnowledgeReconciliationDecision,
} from "./contracts"

/** Stages individual or batch knowledge-version choices until one explicit commit. */
export function DesktopKnowledgeReconciliationPanel({ refreshKey = 0 }: { refreshKey?: number }) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [conflicts, setConflicts] = useState<DesktopKnowledgeReconciliationConflict[]>([])
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

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
  }, [bridge, refreshKey])

  const selectedConflicts = useMemo(
    () => conflicts.filter((conflict) => selectedIds.includes(conflict.candidateId)),
    [conflicts, selectedIds],
  )
  const stagedCount = conflicts.filter((conflict) => conflict.stagedDecision !== null).length
  const allSelected = Boolean(conflicts.length) && selectedConflicts.length === conflicts.length
  const selectedMode = selectedConflicts.length
    && selectedConflicts.every((conflict) => (
      conflict.reconciliationMode === selectedConflicts[0]?.reconciliationMode
    ))
    ? selectedConflicts[0]?.reconciliationMode
    : null

  const replaceConflicts = (next: DesktopKnowledgeReconciliationConflict[]) => {
    setConflicts(next)
    const remaining = new Set(next.map((conflict) => conflict.candidateId))
    setSelectedIds((current) => current.filter((candidateId) => remaining.has(candidateId)))
  }

  const stage = async (
    candidateIds: string[],
    decision: DesktopKnowledgeReconciliationDecision | null,
    manualMergeContent: string | null = null,
  ) => {
    if (!candidateIds.length) return
    setWorking(true)
    setError(null)
    setSaved(null)
    try {
      const result = await bridge.stageKnowledgeReconciliationDecisions(
        candidateIds,
        decision,
        manualMergeContent,
        nextDesktopRequestId("knowledge-reconciliation"),
      )
      replaceConflicts(result.conflicts)
      setSaved(
        decision === null
          ? t("desktop.knowledgeBases.reconciliation.choiceCleared")
          : t("desktop.knowledgeBases.reconciliation.choiceStaged"),
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setWorking(false)
    }
  }

  const commit = async () => {
    if (!stagedCount) return
    setWorking(true)
    setError(null)
    setSaved(null)
    try {
      const result = await bridge.commitKnowledgeReconciliationDecisions(
        nextDesktopRequestId("knowledge-reconciliation"),
      )
      const resolved = new Set(result.resolvedCandidateIds)
      replaceConflicts(conflicts.filter((conflict) => !resolved.has(conflict.candidateId)))
      setSaved(t("desktop.knowledgeBases.reconciliation.commitSaved", {
        published: result.publishedCount,
        draftUpdated: result.draftUpdatedCount,
        kept: result.keptCount,
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
    <section className="mt-8 max-w-4xl" data-testid="desktop-knowledge-reconciliation-conflicts">
      <div className="rounded-apple-lg border border-border/70 bg-background p-5 shadow-sm md:p-6">
        <div className="flex items-start gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-amber-500/10 text-amber-700 dark:text-amber-300">
            <GitPullRequest className="size-4" />
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-semibold">{t("desktop.knowledgeBases.reconciliation.title")}</h2>
              {stagedCount ? (
                <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                  {t("desktop.knowledgeBases.reconciliation.stagedCount", { count: stagedCount })}
                </span>
              ) : null}
            </div>
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
        {saved ? (
          <p className="mt-5 rounded-lg border border-emerald-600/25 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">
            {saved}
          </p>
        ) : null}
        {loading ? (
          <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("desktop.knowledgeBases.reconciliation.loading")}
          </div>
        ) : conflicts.length ? (
          <>
            <div className="mt-6 flex flex-wrap items-center gap-2 rounded-xl border border-border/70 bg-muted/20 p-3">
              <label className="flex items-center gap-2 text-sm text-muted-foreground">
                <input
                  type="checkbox"
                  checked={allSelected}
                  disabled={working}
                  onChange={() => setSelectedIds(allSelected ? [] : conflicts.map((item) => item.candidateId))}
                />
                {t("desktop.knowledgeBases.reconciliation.selectAll")}
              </label>
              <span className="text-xs text-muted-foreground">
                {t("desktop.knowledgeBases.reconciliation.selectedCount", { count: selectedConflicts.length })}
              </span>
              <div className="ml-auto flex flex-wrap gap-2">
                {selectedMode === "two_way" ? (
                  <>
                    <Button
                      size="sm"
                      disabled={working}
                      onClick={() => void stage(selectedConflicts.map((item) => item.candidateId), "publish_incoming")}
                    >
                      {t("desktop.knowledgeBases.reconciliation.publishIncoming")}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={working}
                      onClick={() => void stage(selectedConflicts.map((item) => item.candidateId), "keep_current")}
                    >
                      {t("desktop.knowledgeBases.reconciliation.keepCurrent")}
                    </Button>
                  </>
                ) : null}
                {selectedMode === "three_way" ? (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={working}
                      onClick={() => void stage(selectedConflicts.map((item) => item.candidateId), "keep_draft")}
                    >
                      {t("desktop.knowledgeBases.reconciliation.keepDraft")}
                    </Button>
                    <Button
                      size="sm"
                      disabled={working}
                      onClick={() => void stage(selectedConflicts.map((item) => item.candidateId), "apply_incoming")}
                    >
                      {t("desktop.knowledgeBases.reconciliation.applyIncoming")}
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={working}
                      onClick={() => void stage(selectedConflicts.map((item) => item.candidateId), "replace_draft")}
                    >
                      {t("desktop.knowledgeBases.reconciliation.replaceDraft")}
                    </Button>
                  </>
                ) : null}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={working || !selectedConflicts.length}
                  onClick={() => void stage(selectedConflicts.map((item) => item.candidateId), null)}
                >
                  {t("desktop.knowledgeBases.reconciliation.clearChoice")}
                </Button>
                <Button size="sm" disabled={working || !stagedCount} onClick={() => void commit()}>
                  {working ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
                  {t("desktop.knowledgeBases.reconciliation.commit", { count: stagedCount })}
                </Button>
              </div>
            </div>
            <div className="mt-4 space-y-3">
              {conflicts.map((conflict) => (
                <ConflictCard
                  key={conflict.candidateId}
                  conflict={conflict}
                  disabled={working}
                  selected={selectedIds.includes(conflict.candidateId)}
                  onToggleSelected={() => toggleSelected(conflict.candidateId)}
                  onStage={(decision, manualMergeContent) => (
                    void stage([conflict.candidateId], decision, manualMergeContent)
                  )}
                />
              ))}
            </div>
          </>
        ) : (
          <p className="py-10 text-sm text-muted-foreground">
            {t("desktop.knowledgeBases.reconciliation.empty")}
          </p>
        )}
      </div>
    </section>
  )
}

function ConflictCard({
  conflict,
  disabled,
  selected,
  onToggleSelected,
  onStage,
}: {
  conflict: DesktopKnowledgeReconciliationConflict
  disabled: boolean
  selected: boolean
  onToggleSelected: () => void
  onStage: (
    decision: DesktopKnowledgeReconciliationDecision | null,
    manualMergeContent?: string | null,
  ) => void
}) {
  const { t } = useTranslation("common")
  const [manualMerge, setManualMerge] = useState(
    conflict.stagedContentMarkdown
      ?? conflict.workingDraftContentMarkdown
      ?? conflict.contentMarkdown,
  )
  const decisionLabel = stagedDecisionLabel(t, conflict.stagedDecision)
  const isThreeWay = conflict.reconciliationMode === "three_way"
  return (
    <article className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <input
          type="checkbox"
          checked={selected}
          disabled={disabled}
          onChange={onToggleSelected}
          aria-label={t("desktop.knowledgeBases.reconciliation.selectConflict", { title: conflict.title })}
        />
        <p className="font-medium">{conflict.title}</p>
        <span className="rounded-full bg-background px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
          {t(`desktop.knowledgeBases.reconciliation.${conflict.kind}`)}
        </span>
        {decisionLabel ? (
          <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
            {decisionLabel}
          </span>
        ) : null}
      </div>
      <p className="mt-1 text-sm text-muted-foreground">
        {t("desktop.knowledgeBases.reconciliation.fromDocument", {
          document: conflict.documentName,
        })}
      </p>
      <div className={`mt-4 grid gap-3 ${isThreeWay ? "lg:grid-cols-3" : "sm:grid-cols-2"}`}>
        <ConflictExcerpt
          label={conflict.baselineKind !== "published_generation"
            ? t("desktop.knowledgeBases.reconciliation.currentPublishedRevision")
            : t("desktop.knowledgeBases.reconciliation.publishedKnowledge")}
          content={conflict.baselineKind === "unpublished_page"
            ? t("desktop.knowledgeBases.reconciliation.noPublishedRevision")
            : conflict.baselineContentMarkdown}
        />
        {isThreeWay ? (
          <ConflictExcerpt
            label={t("desktop.knowledgeBases.reconciliation.workingDraft")}
            content={conflict.workingDraftContentMarkdown ?? ""}
            baselineContent={conflict.baselineContentMarkdown}
          />
        ) : null}
        <ConflictExcerpt
          label={t("desktop.knowledgeBases.reconciliation.incoming")}
          content={conflict.contentMarkdown}
          baselineContent={conflict.baselineContentMarkdown}
        />
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {isThreeWay ? (
          <>
            <Button size="sm" variant="outline" disabled={disabled} onClick={() => onStage("keep_draft")}>
              {t("desktop.knowledgeBases.reconciliation.keepDraft")}
            </Button>
            <Button size="sm" disabled={disabled} onClick={() => onStage("apply_incoming")}>
              {t("desktop.knowledgeBases.reconciliation.applyIncoming")}
            </Button>
            <Button size="sm" variant="destructive" disabled={disabled} onClick={() => onStage("replace_draft")}>
              {t("desktop.knowledgeBases.reconciliation.replaceDraft")}
            </Button>
          </>
        ) : (
          <>
            <Button size="sm" disabled={disabled} onClick={() => onStage("publish_incoming")}>
              {t("desktop.knowledgeBases.reconciliation.publishIncoming")}
            </Button>
            <Button size="sm" variant="outline" disabled={disabled} onClick={() => onStage("keep_current")}>
              {t("desktop.knowledgeBases.reconciliation.keepCurrent")}
            </Button>
          </>
        )}
        {conflict.stagedDecision ? (
          <Button size="sm" variant="ghost" disabled={disabled} onClick={() => onStage(null)}>
            <RotateCcw className="size-3.5" />
            {t("desktop.knowledgeBases.reconciliation.clearChoice")}
          </Button>
        ) : null}
      </div>
      {isThreeWay ? (
        <div className="mt-4 rounded-lg border border-border/70 bg-background/70 p-3">
          <label className="text-xs font-medium text-muted-foreground" htmlFor={`manual-merge-${conflict.candidateId}`}>
            {t("desktop.knowledgeBases.reconciliation.manualMerge")}
          </label>
          <Textarea
            id={`manual-merge-${conflict.candidateId}`}
            className="mt-2 min-h-28 font-mono text-xs"
            disabled={disabled}
            value={manualMerge}
            onChange={(event) => setManualMerge(event.target.value)}
          />
          <Button
            className="mt-2"
            size="sm"
            variant="outline"
            disabled={disabled}
            onClick={() => onStage("manual_merge", manualMerge)}
          >
            {t("desktop.knowledgeBases.reconciliation.stageManualMerge")}
          </Button>
        </div>
      ) : null}
    </article>
  )
}

function stagedDecisionLabel(
  t: ReturnType<typeof useTranslation>["t"],
  decision: DesktopKnowledgeReconciliationDecision | null,
): string | null {
  if (decision === null) return null
  const labels: Record<DesktopKnowledgeReconciliationDecision, string> = {
    publish_incoming: "stagedPublish",
    keep_current: "stagedKeep",
    keep_draft: "stagedKeepDraft",
    apply_incoming: "stagedApplyIncoming",
    replace_draft: "stagedReplaceDraft",
    manual_merge: "stagedManualMerge",
  }
  return t(`desktop.knowledgeBases.reconciliation.${labels[decision]}`)
}

function ConflictExcerpt({
  label,
  content,
  baselineContent,
}: {
  label: string
  content: string
  baselineContent?: string
}) {
  const { t } = useTranslation("common")
  const difference = baselineContent === undefined
    ? null
    : summarizeDifference(baselineContent, content)
  return (
    <div className="rounded-lg border border-border/70 bg-muted/20 p-3">
      <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
      {difference ? (
        <p className="mt-1 text-[11px] text-muted-foreground">
          {t("desktop.knowledgeBases.reconciliation.diffSummary", difference)}
        </p>
      ) : null}
      <p className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-sm leading-6">{content}</p>
    </div>
  )
}

function summarizeDifference(baseline: string, value: string): { added: number; removed: number } {
  const baselineLines = new Set(markdownLines(baseline))
  const valueLines = new Set(markdownLines(value))
  return {
    added: [...valueLines].filter((line) => !baselineLines.has(line)).length,
    removed: [...baselineLines].filter((line) => !valueLines.has(line)).length,
  }
}

function markdownLines(value: string): string[] {
  return value.split("\n").map((line) => line.trim()).filter(Boolean)
}
