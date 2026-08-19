/** Grounded retrieval, immutable trace, and answer contracts. */

export interface DesktopRetrievalPlan {
  query: string
  terms: string[]
  source: "deterministic" | "model" | string
}

export interface DesktopEvidenceRef {
  evidenceId: string
  documentId: string
  documentName: string
  section: string
  locator: Record<string, unknown>
  excerpt: string
  channels: string[]
}

export interface DesktopAnswerSourceImage {
  sourceImageId: string
  evidenceId: string
  documentId: string
  documentName: string
  name: string
  mediaType: string
  filePath: string
  altText: string | null
  locator: Record<string, unknown>
}

export interface DesktopRetrievalChannelTrace {
  channel: string
  candidateCount: number
  triggerReasons: string[]
  degradationReasons: string[]
}

export interface DesktopRetrievalTrace {
  catalogGenerationIds: string[]
  pageTreeGenerationIds: string[]
  channels: DesktopRetrievalChannelTrace[]
  triggerReasons: string[]
  degradationReasons: string[]
  selectedNodeIds: string[]
  canonicalEvidenceIds: string[]
  fusionPolicyVersion: string
}

export interface DesktopGroundedAnswer {
  answerId: string
  question: string
  answerText: string
  retrievalPlan: DesktopRetrievalPlan
  citations: DesktopEvidenceRef[]
  sourceImages: DesktopAnswerSourceImage[]
  retrievalTrace: DesktopRetrievalTrace
  degradations: string[]
  status: "completed" | "interrupted"
  interruptionCode: string | null
  interruptionReason: string | null
  createdAt: string
}

export interface DesktopGroundedAnswers {
  answers: DesktopGroundedAnswer[]
}
