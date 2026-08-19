import type {
  DesktopKnowledgePage,
  DesktopKnowledgePageKind,
  DesktopKnowledgePages,
  DesktopKnowledgeSourceCandidate,
  DesktopKnowledgeSourceMapEntry,
} from "./contracts"

const MEMORY_SOURCE: DesktopKnowledgeSourceCandidate = {
  evidenceId: "0123456789abcdef-memory-evidence",
  documentId: "memory-document",
  documentName: "OpenKB Guide.md",
  section: "Knowledge sources",
  locator: { line_start: 1, line_end: 1 },
  excerpt: "Published claims can route only to Available original evidence.",
}

/** Deterministic draft/publication state for the renderer-only bridge. */
export class MemoryKnowledgePageStore {
  private pages: DesktopKnowledgePage[] = []
  private selectedPageId: string | null = null

  reset(): void {
    this.pages = []
    this.selectedPageId = null
  }

  list(): DesktopKnowledgePages {
    return {
      pages: this.pages.map(({ pageId, kind, title, publicationState, publishedRevisionNumber, updatedAt }) => ({
        pageId,
        kind,
        title,
        publicationState,
        publishedRevisionNumber,
        updatedAt,
      })),
      selectedPageId: this.selectedPageId,
    }
  }

  get(pageId: string): DesktopKnowledgePage {
    const page = this.require(pageId)
    this.selectedPageId = pageId
    return page
  }

  saveDraft(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): DesktopKnowledgePage {
    const existing = pageId === undefined ? undefined : this.require(pageId)
    if (existing && existing.kind !== kind) throw new Error("Knowledge page type cannot change.")
    const now = new Date().toISOString()
    const resolvedPageId = existing?.pageId ?? `knowledge-page-${requestId}`
    const sourceMap = existing?.workingDraft?.sourceMap ?? existing?.publishedRevision?.sourceMap ?? []
    const publicationDiagnostics = diagnostics(contentMarkdown, sourceMap)
    const page: DesktopKnowledgePage = {
      pageId: resolvedPageId,
      kind,
      title: title.trim(),
      publicationState: existing?.publishedRevision ? "unpublished_changes" : "draft",
      publishedRevisionNumber: existing?.publishedRevision?.revisionNumber ?? null,
      materializedPath: existing?.materializedPath ?? `knowledge-pages/${kind}/${resolvedPageId}.md`,
      updatedAt: now,
      publishedRevision: existing?.publishedRevision ?? null,
      verification: existing?.verification
        ? { ...existing.verification, canVerify: false, reason: "working_draft_not_verifiable" }
        : unverified("publish_required", false),
      workingDraft: {
        title: title.trim(),
        contentMarkdown,
        updatedAt: now,
        provenanceState: draftProvenance(sourceMap, publicationDiagnostics),
        sourceMap,
      },
      publicationDiagnostics,
    }
    this.pages = existing
      ? this.pages.map((candidate) => candidate.pageId === resolvedPageId ? page : candidate)
      : [page, ...this.pages]
    this.selectedPageId = resolvedPageId
    return page
  }

  publish(pageId: string): DesktopKnowledgePage {
    const current = this.require(pageId)
    if (!current.workingDraft) throw new Error("Save a Working Draft before publishing.")
    if (current.publicationDiagnostics.length) throw new Error(current.publicationDiagnostics[0].message)
    const now = new Date().toISOString()
    const published: DesktopKnowledgePage = {
      ...current,
      title: current.workingDraft.title,
      publicationState: "published",
      publishedRevisionNumber: (current.publishedRevisionNumber ?? 0) + 1,
      updatedAt: now,
      publishedRevision: {
        revisionNumber: (current.publishedRevisionNumber ?? 0) + 1,
        title: current.workingDraft.title,
        contentMarkdown: current.workingDraft.contentMarkdown,
        publishedAt: now,
        provenanceState: current.workingDraft.provenanceState,
        sourceMap: current.workingDraft.sourceMap,
      },
      verification: unverified(current.verification.state === "human_reviewed" ? "revision_changed" : "not_verified", true),
      workingDraft: null,
      publicationDiagnostics: [],
    }
    this.pages = this.pages.map((candidate) => candidate.pageId === pageId ? published : candidate)
    return published
  }

  verify(pageId: string): DesktopKnowledgePage {
    const current = this.require(pageId)
    if (!current.publishedRevision || current.workingDraft) {
      throw new Error("Publish the Working Draft before verifying.")
    }
    const verified: DesktopKnowledgePage = {
      ...current,
      verification: {
        state: "human_reviewed",
        canVerify: false,
        reason: null,
        actor: "local_user",
        verifiedAt: new Date().toISOString(),
        revisionId: `memory-revision-${current.publishedRevision.revisionNumber}`,
      },
    }
    this.pages = this.pages.map((page) => page.pageId === pageId ? verified : page)
    return verified
  }

  searchSources(query: string): DesktopKnowledgeSourceCandidate[] {
    const needle = query.trim().toLowerCase()
    if (!needle) return []
    const haystack = `${MEMORY_SOURCE.documentName} ${MEMORY_SOURCE.section} ${MEMORY_SOURCE.excerpt}`
      .toLowerCase()
    return needle.split(/\s+/).some((term) => haystack.includes(term)) ? [MEMORY_SOURCE] : []
  }

  bindSource(pageId: string, claimText: string, evidenceId: string): DesktopKnowledgePage {
    const current = this.require(pageId)
    if (!current.workingDraft) throw new Error("Save a Working Draft before binding a source.")
    if (evidenceId !== MEMORY_SOURCE.evidenceId) throw new Error("Choose Available evidence.")
    const claim = claimText.trim()
    if (!claim || current.workingDraft.contentMarkdown.split(claim).length !== 2) {
      throw new Error("Select one unique claim from the Working Draft.")
    }
    const sourceId = `src-${evidenceId.slice(0, 16)}`
    const marker = `[^${sourceId}]`
    const contentMarkdown = current.workingDraft.contentMarkdown.includes(marker)
      ? current.workingDraft.contentMarkdown
      : current.workingDraft.contentMarkdown.replace(claim, `${claim}${marker}`)
    const entry: DesktopKnowledgeSourceMapEntry = {
      ...MEMORY_SOURCE,
      sourceId,
      claimText: claim,
      availability: "available",
    }
    const sourceMap = [entry, ...current.workingDraft.sourceMap.filter((item) => item.sourceId !== sourceId)]
    const publicationDiagnostics = diagnostics(contentMarkdown, sourceMap)
    const next: DesktopKnowledgePage = {
      ...current,
      updatedAt: new Date().toISOString(),
      workingDraft: {
        ...current.workingDraft,
        contentMarkdown,
        provenanceState: draftProvenance(sourceMap, publicationDiagnostics),
        sourceMap,
      },
      verification: {
        ...current.verification,
        canVerify: false,
        reason: "working_draft_not_verifiable",
      },
      publicationDiagnostics,
    }
    this.pages = this.pages.map((page) => page.pageId === pageId ? next : page)
    return next
  }

  private require(pageId: string): DesktopKnowledgePage {
    const page = this.pages.find((candidate) => candidate.pageId === pageId)
    if (!page) throw new Error("The requested knowledge page was not found.")
    return page
  }
}

function diagnostics(content: string, sources: DesktopKnowledgeSourceMapEntry[]) {
  return sources
    .filter((source) => !content.includes(`[^${source.sourceId}]`))
    .map((source) => ({
      code: "knowledge_source_marker_missing",
      message: "Restore the source marker before publishing.",
      sourceId: source.sourceId,
    }))
}

function draftProvenance(
  sources: DesktopKnowledgeSourceMapEntry[],
  publicationDiagnostics: ReturnType<typeof diagnostics>,
) {
  if (publicationDiagnostics.length) return sources.length ? "invalid" as const : "unsourced" as const
  return sources.length ? "source_backed" as const : "structural" as const
}

function unverified(reason: "publish_required" | "not_verified" | "revision_changed", canVerify: boolean) {
  return {
    state: "unverified" as const,
    canVerify,
    reason,
    actor: null,
    verifiedAt: null,
    revisionId: null,
  }
}
