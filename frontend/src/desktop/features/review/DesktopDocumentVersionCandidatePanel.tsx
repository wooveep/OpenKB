import { Check, GitCompareArrows, History, Link2, Loader2, Split } from "lucide-react"
import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { useDesktopBridge } from "@/desktop/bridge/context"
import { nextDesktopRequestId } from "@/desktop/shared/request-id"
import type {
  DesktopDocumentVersionCandidate,
  DesktopDocumentVersionCandidateDecision,
  DesktopDocumentLineage,
  DesktopDocumentVersionCatalog,
  DesktopDocumentVersionDiff,
  DesktopSnapshotKind,
  DesktopVersionScheme,
} from "@/desktop/bridge/contracts"

type LineageDraft = {
  displayName: string
  aliases: string
  versionScheme: DesktopVersionScheme
  currentDocumentId: string
  labels: Record<string, string>
  branchLabels: Record<string, string>
  predecessors: Record<string, string>
  snapshotKinds: Record<string, DesktopSnapshotKind>
}

/** Lets a person decide whether a D3 suggestion belongs to an existing source. */
export function DesktopDocumentVersionCandidatePanel({
  onOpenOriginal,
}: {
  onOpenOriginal?: (documentId: string, locator: Record<string, unknown>) => void
}) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [candidates, setCandidates] = useState<DesktopDocumentVersionCandidate[]>([])
  const [loading, setLoading] = useState(true)
  const [resolvingId, setResolvingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [catalog, setCatalog] = useState<DesktopDocumentVersionCatalog | null>(null)
  const [drafts, setDrafts] = useState<Record<string, LineageDraft>>({})
  const [diffs, setDiffs] = useState<Record<string, DesktopDocumentVersionDiff[]>>({})
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  const refreshCatalog = useCallback(async () => {
    const next = await bridge.documentVersionCatalog()
    setCatalog(next)
    setDrafts((current) => Object.fromEntries(next.lineages.map((lineage) => [
      lineage.lineageId,
      current[lineage.lineageId] ?? draftForLineage(lineage),
    ])))
    const confirmed = next.lineages.filter((lineage) => lineage.lineageState === "confirmed")
    const loadedDiffs = await Promise.all(confirmed.map(async (lineage) => (
      [lineage.lineageId, (await bridge.documentVersionDiffs(lineage.lineageId)).diffs] as const
    )))
    setDiffs(Object.fromEntries(loadedDiffs))
    return next
  }, [bridge])

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

  useEffect(() => {
    let disposed = false
    void Promise.resolve()
      .then(() => refreshCatalog())
      .catch((reason) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { disposed = true }
  }, [refreshCatalog])

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
      await refreshCatalog()
      setCandidates((current) => current.filter((item) => item.documentId !== candidate.documentId))
      setSaved(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setResolvingId(null)
    }
  }

  const updateDraft = (
    lineageId: string,
    update: (draft: LineageDraft) => LineageDraft,
  ) => {
    setDrafts((current) => {
      const draft = current[lineageId]
      return draft ? { ...current, [lineageId]: update(draft) } : current
    })
  }

  const confirmLineage = async (lineage: DesktopDocumentLineage) => {
    const draft = drafts[lineage.lineageId]
    if (!draft) return
    setConfirmingId(lineage.lineageId)
    setError(null)
    setSaved(false)
    try {
      const next = await bridge.confirmDocumentLineage({
        displayName: draft.displayName,
        versionScheme: draft.versionScheme,
        currentDocumentId: draft.currentDocumentId,
        aliases: draft.aliases
          .split(",")
          .map((alias) => alias.trim())
          .filter(Boolean),
        lineageId: lineage.lineageId,
        expectedMetadataRevisions: [{
          lineageId: lineage.lineageId,
          metadataRevision: lineage.metadataRevision,
        }],
        members: lineage.members.map((member) => ({
          documentId: member.documentId,
          versionLabel: draft.labels[member.documentId] ?? "",
          branchLabel: draft.branchLabels[member.documentId] ?? "main",
          predecessorDocumentId: draft.predecessors[member.documentId] || null,
          snapshotKind: draft.snapshotKinds[member.documentId] ?? "full_snapshot",
          metadataOrigin: "user",
        })),
      }, nextDesktopRequestId("document-lineage"))
      setCatalog(next)
      await refreshCatalog()
      setSaved(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setConfirmingId(null)
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

      {catalog ? (
        <div className="mt-5 rounded-apple-lg border border-border/70 bg-background p-5 shadow-sm md:p-6">
          <div className="flex items-start gap-3">
            <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary">
              <History className="size-4" />
            </div>
            <div>
              <h2 className="font-semibold">{t("desktop.knowledgeBases.versionCatalog.title")}</h2>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                {t("desktop.knowledgeBases.versionCatalog.description")}
              </p>
            </div>
          </div>

          <div className="mt-6 space-y-4">
            {catalog.lineages.map((lineage) => lineage.lineageState === "needs_order_review" ? (
              <LineageReview
                key={lineage.lineageId}
                lineage={lineage}
                draft={drafts[lineage.lineageId] ?? draftForLineage(lineage)}
                confirming={confirmingId === lineage.lineageId}
                disabled={confirmingId !== null}
                onUpdate={(update) => updateDraft(lineage.lineageId, update)}
                onConfirm={() => void confirmLineage(lineage)}
              />
            ) : (
              <ConfirmedLineage
                key={lineage.lineageId}
                lineage={lineage}
                diffs={diffs[lineage.lineageId] ?? []}
                onOpenOriginal={onOpenOriginal}
              />
            ))}
          </div>
        </div>
      ) : null}
    </section>
  )
}

function LineageReview({
  lineage,
  draft,
  confirming,
  disabled,
  onUpdate,
  onConfirm,
}: {
  lineage: DesktopDocumentLineage
  draft: LineageDraft
  confirming: boolean
  disabled: boolean
  onUpdate: (update: (draft: LineageDraft) => LineageDraft) => void
  onConfirm: () => void
}) {
  const { t } = useTranslation("common")
  const complete = Boolean(
    draft.displayName.trim()
    && draft.currentDocumentId
    && lineage.members.every((member) => draft.labels[member.documentId]?.trim()),
  )
  return (
    <article className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
      <h3 className="text-sm font-semibold">
        {t("desktop.knowledgeBases.versionCatalog.reviewTitle")}
      </h3>
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <label className="text-xs text-muted-foreground">
          {t("desktop.knowledgeBases.versionCatalog.lineageName")}
          <input
            value={draft.displayName}
            disabled={disabled}
            onChange={(event) => onUpdate((current) => ({
              ...current,
              displayName: event.target.value,
            }))}
            className="mt-1 h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground"
          />
        </label>
        <label className="text-xs text-muted-foreground">
          {t("desktop.knowledgeBases.versionCatalog.scheme")}
          <select
            value={draft.versionScheme}
            disabled={disabled}
            onChange={(event) => onUpdate((current) => ({
              ...current,
              versionScheme: event.target.value as DesktopVersionScheme,
            }))}
            className="mt-1 h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground"
          >
            {(["numeric_dotted", "semver", "calendar", "opaque"] as const).map((scheme) => (
              <option key={scheme} value={scheme}>{scheme}</option>
            ))}
          </select>
        </label>
        <label className="text-xs text-muted-foreground">
          {t("desktop.knowledgeBases.versionCatalog.aliases")}
          <input
            value={draft.aliases}
            disabled={disabled}
            onChange={(event) => onUpdate((current) => ({
              ...current,
              aliases: event.target.value,
            }))}
            className="mt-1 h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground"
          />
        </label>
      </div>
      <div className="mt-4 space-y-3">
        {lineage.members.map((member) => (
          <div key={member.documentId} className="grid gap-2 rounded-lg border border-border/60 bg-background p-3 sm:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_8rem_8rem_9rem_9rem]">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{member.documentName}</p>
              <label className="mt-2 block text-xs text-muted-foreground">
                {t("desktop.knowledgeBases.versionCatalog.versionLabel")}
                <input
                  value={draft.labels[member.documentId] ?? ""}
                  disabled={disabled}
                  onChange={(event) => onUpdate((current) => ({
                    ...current,
                    labels: { ...current.labels, [member.documentId]: event.target.value },
                  }))}
                  className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2 text-sm text-foreground"
                />
              </label>
            </div>
            <label className="text-xs text-muted-foreground">
              {t("desktop.knowledgeBases.versionCatalog.branch")}
              <input
                value={draft.branchLabels[member.documentId] ?? "main"}
                disabled={disabled}
                onChange={(event) => onUpdate((current) => ({
                  ...current,
                  branchLabels: {
                    ...current.branchLabels,
                    [member.documentId]: event.target.value,
                  },
                }))}
                className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2 text-sm text-foreground"
              />
            </label>
            <label className="text-xs text-muted-foreground">
              {t("desktop.knowledgeBases.versionCatalog.current")}
              <select
                value={draft.currentDocumentId === member.documentId ? "yes" : "no"}
                disabled={disabled}
                onChange={(event) => {
                  if (event.target.value === "yes") {
                    onUpdate((current) => ({ ...current, currentDocumentId: member.documentId }))
                  }
                }}
                className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2 text-sm text-foreground"
              >
                <option value="no">{t("desktop.knowledgeBases.versionCatalog.no")}</option>
                <option value="yes">{t("desktop.knowledgeBases.versionCatalog.yes")}</option>
              </select>
            </label>
            <label className="text-xs text-muted-foreground">
              {t("desktop.knowledgeBases.versionCatalog.predecessor")}
              <select
                value={draft.predecessors[member.documentId] ?? ""}
                disabled={disabled}
                onChange={(event) => onUpdate((current) => ({
                  ...current,
                  predecessors: {
                    ...current.predecessors,
                    [member.documentId]: event.target.value,
                  },
                }))}
                className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2 text-sm text-foreground"
              >
                <option value="">{t("desktop.knowledgeBases.versionCatalog.none")}</option>
                {lineage.members.filter((candidate) => candidate.documentId !== member.documentId).map((candidate) => (
                  <option key={candidate.documentId} value={candidate.documentId}>
                    {draft.labels[candidate.documentId] || candidate.documentName}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-muted-foreground">
              {t("desktop.knowledgeBases.versionCatalog.snapshotKind")}
              <select
                value={draft.snapshotKinds[member.documentId] ?? "full_snapshot"}
                disabled={disabled}
                onChange={(event) => onUpdate((current) => ({
                  ...current,
                  snapshotKinds: {
                    ...current.snapshotKinds,
                    [member.documentId]: event.target.value as DesktopSnapshotKind,
                  },
                }))}
                className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2 text-sm text-foreground"
              >
                <option value="full_snapshot">{t("desktop.knowledgeBases.versionCatalog.fullSnapshot")}</option>
                <option value="delta">{t("desktop.knowledgeBases.versionCatalog.delta")}</option>
                <option value="unknown">{t("desktop.knowledgeBases.versionCatalog.unknown")}</option>
              </select>
            </label>
          </div>
        ))}
      </div>
      <Button className="mt-4" size="sm" disabled={disabled || !complete} onClick={onConfirm}>
        {confirming ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
        {t("desktop.knowledgeBases.versionCatalog.confirm")}
      </Button>
    </article>
  )
}

function ConfirmedLineage({
  lineage,
  diffs,
  onOpenOriginal,
}: {
  lineage: DesktopDocumentLineage
  diffs: DesktopDocumentVersionDiff[]
  onOpenOriginal?: (documentId: string, locator: Record<string, unknown>) => void
}) {
  const { t } = useTranslation("common")
  return (
    <article className="rounded-xl border border-border/70 bg-muted/20 p-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="truncate text-sm font-semibold">{lineage.displayName}</h3>
        <span className="text-xs text-muted-foreground">
          {t("desktop.knowledgeBases.versionCatalog.versionCount", { count: lineage.members.length })}
        </span>
      </div>
      <ul className="mt-3 flex flex-wrap gap-2">
        {lineage.members.map((member) => (
          <li key={member.documentId} className="rounded-full border border-border/70 bg-background px-2.5 py-1 text-xs">
            {member.versionLabel ?? t("desktop.knowledgeBases.versionCatalog.unconfirmed")}
            {member.documentId === lineage.currentDocumentId
              ? ` · ${t("desktop.knowledgeBases.versionCatalog.current")}`
              : ""}
          </li>
        ))}
      </ul>
      {diffs.map((diff) => (
        <details key={diff.diffId} className="mt-3 rounded-lg border border-border/60 bg-background p-3 text-xs">
          <summary className="cursor-pointer font-medium">
            {t("desktop.knowledgeBases.versionCatalog.diffSummary", {
              changed: (diff.stats.modified ?? 0) + (diff.stats.added ?? 0) + (diff.stats.removed ?? 0),
              moved: diff.stats.moved ?? 0,
            })}
          </summary>
          <p className="mt-2 text-muted-foreground">{diff.algorithmVersion} · {diff.status}</p>
          <ol className="mt-3 max-h-72 space-y-2 overflow-y-auto">
            {diff.items.map((item, index) => (
              <li key={`${item.oldBlockId ?? "none"}:${item.newBlockId ?? "none"}:${index}`} className="rounded-md border border-border/60 p-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">
                    {item.contentChangeKind}
                    {item.locationChangeKind === "moved" ? " · moved" : ""}
                  </span>
                  <div className="flex flex-wrap gap-1">
                    {item.oldLocator && onOpenOriginal ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="h-7 px-2 text-xs"
                        onClick={() => onOpenOriginal(diff.fromDocumentId, item.oldLocator!)}
                      >
                        {t("desktop.knowledgeBases.versionCatalog.openVersion", {
                          version: versionLabel(lineage, diff.fromDocumentId),
                        })}
                      </Button>
                    ) : null}
                    {item.newLocator && onOpenOriginal ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="h-7 px-2 text-xs"
                        onClick={() => onOpenOriginal(diff.toDocumentId, item.newLocator!)}
                      >
                        {t("desktop.knowledgeBases.versionCatalog.openVersion", {
                          version: versionLabel(lineage, diff.toDocumentId),
                        })}
                      </Button>
                    ) : null}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </details>
      ))}
    </article>
  )
}

function draftForLineage(lineage: DesktopDocumentLineage): LineageDraft {
  return {
    displayName: lineage.displayName,
    aliases: lineage.aliases.join(", "),
    versionScheme: lineage.versionScheme,
    currentDocumentId: lineage.currentDocumentId ?? lineage.members.at(-1)?.documentId ?? "",
    labels: Object.fromEntries(lineage.members.map((member) => [
      member.documentId,
      member.versionLabel ?? suggestedVersionLabel(member.documentName),
    ])),
    branchLabels: Object.fromEntries(lineage.members.map((member) => [
      member.documentId,
      member.branchLabel ?? "main",
    ])),
    predecessors: Object.fromEntries(lineage.members.map((member) => [
      member.documentId,
      member.predecessorDocumentId ?? "",
    ])),
    snapshotKinds: Object.fromEntries(lineage.members.map((member) => [
      member.documentId,
      member.snapshotKind,
    ])),
  }
}

function versionLabel(lineage: DesktopDocumentLineage, documentId: string): string {
  const member = lineage.members.find((item) => item.documentId === documentId)
  return member?.versionLabel ?? member?.documentName ?? documentId
}

function suggestedVersionLabel(documentName: string): string {
  return documentName.match(/(?:^|[_\s-])(v?\d+(?:\.\d+)+)(?=[_.\s-]|$)/i)?.[1] ?? ""
}
