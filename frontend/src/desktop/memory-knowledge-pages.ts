import type {
  DesktopKnowledgePage,
  DesktopKnowledgePageKind,
  DesktopKnowledgePages,
} from "./contracts"

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
    const page: DesktopKnowledgePage = {
      pageId: resolvedPageId,
      kind,
      title: title.trim(),
      publicationState: existing?.publishedRevision ? "unpublished_changes" : "draft",
      publishedRevisionNumber: existing?.publishedRevision?.revisionNumber ?? null,
      materializedPath: existing?.materializedPath ?? `knowledge-pages/${kind}/${resolvedPageId}.md`,
      updatedAt: now,
      publishedRevision: existing?.publishedRevision ?? null,
      workingDraft: { title: title.trim(), contentMarkdown, updatedAt: now },
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
      },
      workingDraft: null,
    }
    this.pages = this.pages.map((candidate) => candidate.pageId === pageId ? published : candidate)
    return published
  }

  private require(pageId: string): DesktopKnowledgePage {
    const page = this.pages.find((candidate) => candidate.pageId === pageId)
    if (!page) throw new Error("The requested knowledge page was not found.")
    return page
  }
}
