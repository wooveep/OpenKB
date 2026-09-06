import type {
  DesktopKnowledgePage,
  DesktopKnowledgePageDeletion,
  DesktopKnowledgePageKind,
  DesktopKnowledgePages,
  DesktopKnowledgeExport,
  DesktopKnowledgeExportMode,
  DesktopKnowledgeExportPreview,
  DesktopKnowledgeSourceCandidate,
  DesktopKnowledgeWorkspace,
  DesktopKnowledgeWorkspaceHistory,
  DesktopKnowledgeWorkspaceItem,
  DesktopKnowledgeWorkspaceItemRequest,
  DesktopKnowledgeAdoptionResult,
  DesktopKnowledgeAdoptionDecision,
  DesktopMissingSourceBinding,
  DesktopMissingSourceCandidates,
  DesktopMissingSourceDismissal,
} from "@/desktop/bridge/contracts"

/** Shared unavailable implementations keep the main production Bridge focused. */
export abstract class UnavailableKnowledgePageBridge {
  protected abstract unavailable<T>(): Promise<T>

  knowledgeWorkspace(query = ""): Promise<DesktopKnowledgeWorkspace> {
    void query
    return this.unavailable()
  }

  getKnowledgeWorkspaceItem(
    item: DesktopKnowledgeWorkspaceItemRequest,
  ): Promise<DesktopKnowledgeWorkspaceItem> {
    void item
    return this.unavailable()
  }

  knowledgeWorkspaceHistory(generationId?: number): Promise<DesktopKnowledgeWorkspaceHistory> {
    void generationId
    return this.unavailable()
  }

  adoptKnowledgeItem(
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
    return this.unavailable()
  }

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

  setKnowledgePageStaleAfter(pageId: string, staleAfter: string | null, requestId: string): Promise<DesktopKnowledgePage> {
    void pageId
    void staleAfter
    void requestId
    return this.unavailable()
  }

  deprecateKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void pageId
    void requestId
    return this.unavailable()
  }

  restoreKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    void pageId
    void requestId
    return this.unavailable()
  }

  permanentlyDeleteKnowledgePage(pageId: string, confirmationPageId: string, requestId: string): Promise<DesktopKnowledgePageDeletion> {
    void pageId
    void confirmationPageId
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

  missingSourceCandidates(): Promise<DesktopMissingSourceCandidates> {
    return this.unavailable()
  }

  bindMissingSourceCandidate(
    candidateId: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopMissingSourceBinding> {
    void candidateId
    void evidenceId
    void requestId
    return this.unavailable()
  }

  dismissMissingSourceCandidates(
    candidateIds: string[],
    requestId: string,
  ): Promise<DesktopMissingSourceDismissal> {
    void candidateIds
    void requestId
    return this.unavailable()
  }

  exportKnowledgeBundle(
    destination: string,
    mode: DesktopKnowledgeExportMode,
    requestId: string,
    expectedSnapshotId?: string,
  ): Promise<DesktopKnowledgeExport> {
    void destination
    void mode
    void requestId
    void expectedSnapshotId
    return this.unavailable()
  }

  previewKnowledgeBundle(
    mode: DesktopKnowledgeExportMode,
  ): Promise<DesktopKnowledgeExportPreview> {
    void mode
    return this.unavailable()
  }
}
