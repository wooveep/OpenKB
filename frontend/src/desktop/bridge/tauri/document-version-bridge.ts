import type {
  DesktopDocumentLineageDecision,
  DesktopDocumentVersionCandidate,
  DesktopDocumentVersionCandidateDecision,
  DesktopDocumentVersionCandidates,
  DesktopDocumentVersionCatalog,
  DesktopDocumentVersionDiffs,
} from "@/desktop/bridge/contracts"
import { TauriKnowledgePageBridge } from "@/desktop/bridge/tauri/knowledge-page-bridge"

/** Typed Tauri adapter for user-reviewed Document Lineages and deterministic Diffs. */
export abstract class TauriDocumentVersionBridge extends TauriKnowledgePageBridge {
  async documentVersionCandidates(): Promise<DesktopDocumentVersionCandidates> {
    return this.call<DesktopDocumentVersionCandidates>("desktop_document_version_candidates")
  }

  async documentVersionCatalog(): Promise<DesktopDocumentVersionCatalog> {
    return this.call<DesktopDocumentVersionCatalog>("desktop_document_version_catalog")
  }

  async confirmDocumentLineage(
    decision: DesktopDocumentLineageDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCatalog> {
    return this.call<DesktopDocumentVersionCatalog>("desktop_confirm_document_lineage", {
      decision,
      requestId,
    })
  }

  async documentVersionDiffs(lineageId: string): Promise<DesktopDocumentVersionDiffs> {
    return this.call<DesktopDocumentVersionDiffs>("desktop_document_version_diffs", {
      lineageId,
    })
  }

  async resolveDocumentVersionCandidate(
    candidateId: string,
    decision: DesktopDocumentVersionCandidateDecision,
    requestId: string,
  ): Promise<DesktopDocumentVersionCandidate> {
    return this.call<DesktopDocumentVersionCandidate>(
      "desktop_resolve_document_version_candidate",
      { candidateId, decision, requestId },
    )
  }
}
