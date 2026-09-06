import type {
  DesktopKnowledgeAdoptionDecision,
  DesktopKnowledgeAdoptionResult,
  DesktopKnowledgePage,
  DesktopKnowledgePageDeletion,
  DesktopKnowledgePageKind,
  DesktopKnowledgePages,
  DesktopKnowledgeSourceCandidate,
  DesktopKnowledgeWorkspace,
  DesktopKnowledgeWorkspaceHistory,
  DesktopKnowledgeWorkspaceItem,
  DesktopKnowledgeWorkspaceItemRequest,
} from "@/desktop/bridge/contracts"
import { TauriKnowledgeReanalysisBridge } from "@/desktop/bridge/tauri/knowledge-reanalysis-bridge"

/** Typed Tauri calls for the additive Generated and User Knowledge Workspace. */
export abstract class TauriKnowledgePageBridge extends TauriKnowledgeReanalysisBridge {
  async knowledgePages(): Promise<DesktopKnowledgePages> {
    return this.call<DesktopKnowledgePages>("desktop_knowledge_pages")
  }

  async knowledgeWorkspace(query = ""): Promise<DesktopKnowledgeWorkspace> {
    return this.call<DesktopKnowledgeWorkspace>("desktop_knowledge_workspace", { query })
  }

  async getKnowledgeWorkspaceItem(
    item: DesktopKnowledgeWorkspaceItemRequest,
  ): Promise<DesktopKnowledgeWorkspaceItem> {
    const request = item.authority === "generated"
      ? { authority: item.authority, generationId: item.generationId, itemKey: item.itemKey }
      : { authority: item.authority, pageId: item.pageId }
    return this.call<DesktopKnowledgeWorkspaceItem>("desktop_get_knowledge_workspace_item", {
      item: request,
    })
  }

  async knowledgeWorkspaceHistory(
    generationId?: number,
  ): Promise<DesktopKnowledgeWorkspaceHistory> {
    return this.call<DesktopKnowledgeWorkspaceHistory>(
      "desktop_knowledge_workspace_history",
      { generationId },
    )
  }

  async adoptKnowledgeItem(
    generationId: number,
    itemKey: string,
    adoptionRequestId: string,
    requestId: string,
    decision?: DesktopKnowledgeAdoptionDecision,
    candidatePageId?: string,
  ): Promise<DesktopKnowledgeAdoptionResult> {
    return this.call<DesktopKnowledgeAdoptionResult>("desktop_adopt_knowledge_item", {
      generationId,
      itemKey,
      adoptionRequestId,
      requestId,
      decision,
      candidatePageId,
    })
  }

  async getKnowledgePage(pageId: string): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_get_knowledge_page", { pageId })
  }

  async saveKnowledgePage(
    pageId: string | undefined,
    kind: DesktopKnowledgePageKind,
    title: string,
    contentMarkdown: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_save_knowledge_page", {
      pageId,
      kind,
      title,
      contentMarkdown,
      requestId,
    })
  }

  async publishKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_publish_knowledge_page", { pageId, requestId })
  }

  async verifyKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_verify_knowledge_page", { pageId, requestId })
  }

  async setKnowledgePageStaleAfter(pageId: string, staleAfter: string | null, requestId: string): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_set_knowledge_page_stale_after", { pageId, staleAfter, requestId })
  }

  async deprecateKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_deprecate_knowledge_page", { pageId, requestId })
  }

  async restoreKnowledgePage(pageId: string, requestId: string): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_restore_knowledge_page", { pageId, requestId })
  }

  async permanentlyDeleteKnowledgePage(pageId: string, confirmationPageId: string, requestId: string): Promise<DesktopKnowledgePageDeletion> {
    return this.call<DesktopKnowledgePageDeletion>("desktop_permanently_delete_knowledge_page", {
      pageId,
      confirmationPageId,
      requestId,
    })
  }

  async searchKnowledgeSources(query: string): Promise<DesktopKnowledgeSourceCandidate[]> {
    const result = await this.call<{ sources: DesktopKnowledgeSourceCandidate[] }>(
      "desktop_search_knowledge_sources",
      { query },
    )
    return result.sources
  }

  async bindKnowledgePageSource(
    pageId: string,
    claimText: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopKnowledgePage> {
    return this.call<DesktopKnowledgePage>("desktop_bind_knowledge_page_source", {
      pageId,
      claimText,
      evidenceId,
      requestId,
    })
  }
}
