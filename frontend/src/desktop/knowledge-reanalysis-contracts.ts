export type DesktopDocumentAnalysisState = "current" | "analysis_outdated" | "missing"
export type DesktopKnowledgeReanalysisStatus = "pending" | "running" | "completed" | "partial_failure" | "failed"

export interface DesktopDocumentAnalysisStatus {
  documentId: string
  documentName: string
  state: DesktopDocumentAnalysisState
  schemaVersion: string | null
  provider: string | null
  model: string | null
  promptDigest: string | null
  engineVersion: string | null
  analyzedAt: string | null
}

export interface DesktopKnowledgeReanalysisJob {
  jobId: string
  runId: string
  documentId: string
  documentName: string
  status: Exclude<DesktopKnowledgeReanalysisStatus, "partial_failure">
  phase: "pending" | "batches" | "merge" | "reconciliation" | "completed" | "failed"
  progress: number
  provider: string
  model: string
  errorCode: string | null
  reason: string | null
  batchTotal: number
  batchCompleted: number
  currentBatch: number | null
  attemptCount: number | null
  createdAt: string
  completedAt: string | null
}

export interface DesktopKnowledgeReanalysisRun {
  runId: string
  mode: "single" | "bulk"
  status: DesktopKnowledgeReanalysisStatus
  total: number
  completed: number
  failed: number
  jobs: DesktopKnowledgeReanalysisJob[]
  createdAt: string
  completedAt: string | null
}

export interface DesktopKnowledgeReanalysisOverview {
  documents: DesktopDocumentAnalysisStatus[]
  runs: DesktopKnowledgeReanalysisRun[]
}
