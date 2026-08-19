import type {
  DesktopKnowledgePage,
  DesktopKnowledgePageKind,
  DesktopKnowledgePages,
  DesktopKnowledgeSourceCandidate,
} from "./contracts"

/** Shared unavailable implementations keep the main production Bridge focused. */
export abstract class UnavailableKnowledgePageBridge {
  protected abstract unavailable<T>(): Promise<T>

  knowledgePages(): Promise<DesktopKnowledgePages> {
    return this.unavailable()
  }

  getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage> {
    void pageId
    return this.unavailable()
  }

  saveKnowledgePage(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    void pageId
    void kind
    void title
    void contentMarkdown
    void requestId
    return this.unavailable()
  }

  publishKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void pageId
    void requestId
    return this.unavailable()
  }

  verifyKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void pageId
    void requestId
    return this.unavailable()
  }

  searchKnowledgeSources(query: string): Promise<DesktopKnowledgeSourceCandidate[]> {
    void query
    return this.unavailable()
  }

  bindKnowledgePageSource(
    pageId: string,
    claimText: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    void pageId
    void claimText
    void evidenceId
    void requestId
    return this.unavailable()
  }
}
