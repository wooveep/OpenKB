import type { SemanticReviews, SemanticReviewDecision } from "@/desktop/bridge/contracts/semantic-reviews"
import type {
  DesktopKnowledgeReconciliationCommit,
  DesktopKnowledgeReconciliationConflicts,
  DesktopKnowledgeReconciliationDecision,
  DesktopMissingSourceBinding,
  DesktopMissingSourceCandidates,
  DesktopMissingSourceDismissal,
} from "@/desktop/bridge/contracts"

/** Typed Tauri calls for the two categories that share the Knowledge Review queue. */
export abstract class TauriKnowledgeReviewBridge {
  protected abstract call<T>(command: string, args?: Record<string, unknown>): Promise<T>

  async semanticReviews(): Promise<SemanticReviews> {
    return this.call("desktop_semantic_reviews")
  }

  async resolveSemanticReview(reviewId: string, decision: SemanticReviewDecision, requestId: string): Promise<SemanticReviews> {
    return this.call("desktop_resolve_semantic_review", { reviewId, decision, requestId })
  }

  async knowledgeReconciliationConflicts(): Promise<DesktopKnowledgeReconciliationConflicts> {
    return this.call("desktop_knowledge_reconciliation_conflicts")
  }

  async stageKnowledgeReconciliationDecisions(
    candidateIds: string[],
    decision: DesktopKnowledgeReconciliationDecision | null,
    manualMergeContent: string | null,
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationConflicts> {
    return this.call("desktop_stage_knowledge_reconciliation_decisions", {
      candidateIds,
      decision,
      manualMergeContent,
      requestId,
    })
  }

  async commitKnowledgeReconciliationDecisions(
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationCommit> {
    return this.call("desktop_commit_knowledge_reconciliation_decisions", { requestId })
  }

  async missingSourceCandidates(): Promise<DesktopMissingSourceCandidates> {
    return this.call("desktop_missing_source_candidates")
  }

  async bindMissingSourceCandidate(
    candidateId: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopMissingSourceBinding> {
    return this.call("desktop_bind_missing_source_candidate", {
      candidateId,
      evidenceId,
      requestId,
    })
  }

  async dismissMissingSourceCandidates(
    candidateIds: string[],
    requestId: string,
  ): Promise<DesktopMissingSourceDismissal> {
    return this.call("desktop_dismiss_missing_source_candidates", { candidateIds, requestId })
  }
}
