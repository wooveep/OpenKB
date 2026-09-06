export type SemanticReviewDecision = "compatible" | "same_identity" | "keep_separate" | "keep_current" | "conflicting" | "unresolved"

export interface SemanticReview {
  reviewId: string
  reason: string
  status: "pending" | "resolved"
  decision: SemanticReviewDecision | null
  authority: "model" | "human" | null
  choices: SemanticReviewDecision[]
  candidates: {
    candidateId: string
    candidateGenerationId: string
    documentId: string
    title: string
    kind: string
    aliases: string[]
    claims: { text: string; applicability: [string, string][]; evidenceIds: string[] }[]
  }[]
  evidence: { evidenceId: string; text: string }[]
}

export interface SemanticReviews { items: SemanticReview[] }
