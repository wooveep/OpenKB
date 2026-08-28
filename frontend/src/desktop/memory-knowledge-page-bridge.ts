import type {
  DesktopKnowledgePage,
  DesktopKnowledgePageDeletion,
  DesktopKnowledgePageKind,
  DesktopKnowledgePages,
  DesktopKnowledgeExport,
  DesktopKnowledgeExportMode,
  DesktopKnowledgeSourceCandidate,
  DesktopKnowledgeWorkspace,
  DesktopKnowledgeWorkspaceHistory,
  DesktopKnowledgeWorkspaceItem,
  DesktopKnowledgeWorkspaceItemRequest,
  DesktopKnowledgeAdoptionResult,
  DesktopKnowledgeAdoptionDecision,
} from "./contracts"
import { MemoryKnowledgePageStore } from "./memory-knowledge-pages"

/** Knowledge-page behavior shared by the renderer-only Desktop Bridge. */
export abstract class MemoryKnowledgePageBridge {
  private readonly knowledgePagesStore = new MemoryKnowledgePageStore()

  protected abstract knowledgePagesAvailable(): boolean

  protected resetKnowledgePages(): void {
    this.knowledgePagesStore.reset()
  }

  async knowledgePages(): Promise<DesktopKnowledgePages> {
    return this.knowledgePagesStore.list()
  }

  async knowledgeWorkspace(query = ""): Promise<DesktopKnowledgeWorkspace> {
    const needle = query.trim().toLocaleLowerCase()
    const pages = this.knowledgePagesStore.list().pages
    return {
      currentGenerationId: null,
      items: pages
        .filter((page) => !needle || page.title.toLocaleLowerCase().includes(needle))
        .map((page) => ({
          authority: "user" as const,
          identity: `user:${page.pageId}`,
          kind: page.kind,
          title: page.title,
          updatedAt: page.updatedAt,
          current: true,
          pageId: page.pageId,
          publicationState: page.publicationState,
          lifecycleState: page.lifecycleState,
        })),
    }
  }

  async getKnowledgeWorkspaceItem(
    item: DesktopKnowledgeWorkspaceItemRequest,
  ): Promise<DesktopKnowledgeWorkspaceItem> {
    if (item.authority !== "user") {
      throw new Error("The renderer-only bridge has no generated Knowledge snapshot.")
    }
    return {
      authority: "user",
      identity: `user:${item.pageId}`,
      editable: true,
      ...this.knowledgePagesStore.get(item.pageId),
    }
  }

  async knowledgeWorkspaceHistory(
    generationId?: number,
  ): Promise<DesktopKnowledgeWorkspaceHistory> {
    if (generationId !== undefined) {
      throw new Error("The renderer-only bridge has no generated Knowledge history.")
    }
    return { currentGenerationId: null, generations: [] }
  }

  async adoptKnowledgeItem(
    generationId: number,
    itemKey: string,
    adoptionRequestId: string,
    requestId: string,
    decision?: DesktopKnowledgeAdoptionDecision,
    candidatePageId?: string,
  ): Promise<DesktopKnowledgeAdoptionResult> {
    void generationId
    void itemKey
    void adoptionRequestId
    void requestId
    void decision
    void candidatePageId
    throw new Error("The renderer-only bridge has no generated Knowledge item to adopt.")
  }

  async getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage> {
    return this.knowledgePagesStore.get(pageId)
  }

  async saveKnowledgePage(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    if (!this.knowledgePagesAvailable()) {
      throw new Error("Open a Desktop Knowledge Base before editing knowledge pages.")
    }
    return this.knowledgePagesStore.saveDraft(pageId, kind, title, contentMarkdown, requestId)
  }

  async publishKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void requestId
    return this.knowledgePagesStore.publish(pageId)
  }

  async verifyKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void requestId
    return this.knowledgePagesStore.verify(pageId)
  }

  async setKnowledgePageStaleAfter(
    pageId: string,
    staleAfter: string | null,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    void requestId
    return this.knowledgePagesStore.setStaleAfter(pageId, staleAfter)
  }

  async deprecateKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void requestId
    return this.knowledgePagesStore.deprecate(pageId)
  }

  async restoreKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void requestId
    return this.knowledgePagesStore.restore(pageId)
  }

  async permanentlyDeleteKnowledgePage(
    pageId: string,
    confirmationPageId: string,
    requestId: string,
  ): Promise<DesktopKnowledgePageDeletion> {
    void requestId
    this.knowledgePagesStore.permanentDelete(pageId, confirmationPageId)
    return { pageId, deleted: true }
  }

  async searchKnowledgeSources(query: string): Promise<DesktopKnowledgeSourceCandidate[]> {
    return this.knowledgePagesStore.searchSources(query)
  }

  async bindKnowledgePageSource(
    pageId: string,
    claimText: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    void requestId
    return this.knowledgePagesStore.bindSource(pageId, claimText, evidenceId)
  }

  async exportKnowledgeBundle(
    destination: string,
    mode: DesktopKnowledgeExportMode,
    requestId: string,
  ): Promise<DesktopKnowledgeExport> {
    void requestId
    return {
      path: `${destination}/OpenKB-Knowledge-Export`,
      mode,
      files: ["index.md", "log.md", "source-manifest.json"],
      rawAssetCount: mode === "self_contained" ? 1 : 0,
      sourceImageCount: mode === "self_contained" ? 1 : 0,
    }
  }
}
