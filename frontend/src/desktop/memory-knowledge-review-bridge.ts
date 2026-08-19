import type {
  DesktopKnowledgeReconciliationCommit,
  DesktopKnowledgeReconciliationConflict,
  DesktopKnowledgeReconciliationConflicts,
  DesktopKnowledgeReconciliationDecision,
  DesktopMissingSourceBinding,
  DesktopMissingSourceCandidate,
  DesktopMissingSourceCandidates,
  DesktopMissingSourceDismissal,
} from "./contracts"
import { MemoryKnowledgePageBridge } from "./memory-knowledge-page-bridge"

/** Renderer-only review queues used without Tauri or SQLite. */
export abstract class MemoryKnowledgeReviewBridge extends MemoryKnowledgePageBridge {
  private knowledgeReconciliationConflictResults: DesktopKnowledgeReconciliationConflict[] = []
  private missingSourceCandidateResults: DesktopMissingSourceCandidate[] = []

  async knowledgeReconciliationConflicts(): Promise<DesktopKnowledgeReconciliationConflicts> {
    return { conflicts: this.knowledgeReconciliationConflictResults }
  }

  async stageKnowledgeReconciliationDecisions(
    candidateIds: string[],
    decision: DesktopKnowledgeReconciliationDecision | null,
    manualMergeContent: string | null,
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationConflicts> {
    void requestId
    const selected = new Set(candidateIds)
    if (!selected.size) throw new Error("Choose one or more knowledge conflicts first.")
    this.knowledgeReconciliationConflictResults = this.knowledgeReconciliationConflictResults.map(
      (conflict) => selected.has(conflict.candidateId) ? {
        ...conflict,
        stagedDecision: decision,
        stagedContentMarkdown: decision === "manual_merge" ? manualMergeContent : null,
      } : conflict,
    )
    return this.knowledgeReconciliationConflicts()
  }

  async commitKnowledgeReconciliationDecisions(
    requestId: string,
  ): Promise<DesktopKnowledgeReconciliationCommit> {
    void requestId
    const staged = this.knowledgeReconciliationConflictResults.filter(
      (conflict) => conflict.stagedDecision !== null,
    )
    if (!staged.length) throw new Error("Choose at least one knowledge conflict before committing.")
    const published = staged.filter((item) => item.stagedDecision === "publish_incoming")
    const draftUpdated = staged.filter((item) => (
      item.stagedDecision === "apply_incoming"
      || item.stagedDecision === "replace_draft"
      || item.stagedDecision === "manual_merge"
    ))
    this.knowledgeReconciliationConflictResults = this.knowledgeReconciliationConflictResults
      .filter((item) => item.stagedDecision === null)
    return {
      publishedGenerationId: published.length ? 1 : null,
      publishedCount: published.length,
      draftUpdatedCount: draftUpdated.length,
      keptCount: staged.length - published.length - draftUpdated.length,
      resolvedCandidateIds: staged.map((item) => item.candidateId),
    }
  }

  async missingSourceCandidates(): Promise<DesktopMissingSourceCandidates> {
    return { candidates: this.missingSourceCandidateResults }
  }

  async bindMissingSourceCandidate(
    candidateId: string,
    evidenceId: string,
    requestId: string,
  ): Promise<DesktopMissingSourceBinding> {
    void evidenceId
    void requestId
    const before = this.missingSourceCandidateResults.length
    this.missingSourceCandidateResults = this.missingSourceCandidateResults.filter(
      (candidate) => candidate.candidateId !== candidateId,
    )
    if (before === this.missingSourceCandidateResults.length) {
      throw new Error("The selected Missing Source Candidate was not found.")
    }
    return {
      candidateId,
      decision: "bound",
      outcome: "generated",
      remainingCount: this.missingSourceCandidateResults.length,
    }
  }

  async dismissMissingSourceCandidates(
    candidateIds: string[],
    requestId: string,
  ): Promise<DesktopMissingSourceDismissal> {
    void requestId
    const selected = new Set(candidateIds)
    this.missingSourceCandidateResults = this.missingSourceCandidateResults.filter(
      (candidate) => !selected.has(candidate.candidateId),
    )
    return {
      decision: "dismissed",
      resolvedCandidateIds: candidateIds,
      remainingCount: this.missingSourceCandidateResults.length,
    }
  }
}
