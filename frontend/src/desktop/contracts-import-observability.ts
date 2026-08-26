/** Content-free import progress, model usage, and explicit recovery contracts. */

import type { DesktopModelCallLifecycleStatus } from "./model-call-lifecycle-contracts"

export interface DesktopModelAttempt {
  attempt: number
  status: "running" | "retry_wait" | "completed" | "failed"
  lifecycleStatus: DesktopModelCallLifecycleStatus | null
  elapsedSeconds: number
  errorCode: string | null
  reason: string | null
  finishReason: string | null
  reasoningObserved: boolean | null
  finalContentObserved: boolean | null
  reasoningChunkCount: number | null
  finalChunkCount: number | null
  reasoningCharacterCount: number | null
  finalCharacterCount: number | null
  inputTokens: number | null
  outputTokens: number | null
  totalTokens: number | null
  providerRequestId: string | null
}

export interface DesktopModelCall {
  callId: string
  stageRunId: string
  operation: string
  status: DesktopModelAttempt["status"]
  lifecycleStatus: DesktopModelCallLifecycleStatus | null
  attemptCount: number
  elapsedSeconds: number
  errorCode: string | null
  reason: string | null
  suggestedAction: string | null
  finishReason: string | null
  reasoningObserved: boolean | null
  finalContentObserved: boolean | null
  reasoningChunkCount: number | null
  finalChunkCount: number | null
  reasoningCharacterCount: number | null
  finalCharacterCount: number | null
  inputTokens: number | null
  outputTokens: number | null
  totalTokens: number | null
  providerRequestId: string | null
  attempts: DesktopModelAttempt[]
}

export interface DesktopKnowledgeAnalysisProgress {
  total: number
  completed: number
  active: number
  failed: number
  currentBatch: number | null
  phase: "batches" | "merge" | "completed"
}

/** Optional settings used only by one manual recovery run. */
export interface DesktopRecoveryOverride {
  model?: string
  contextCapacity?: number
  reasoning?: "off" | "low" | "medium" | "high"
  legacyRecoveryChoice?: "continue_compatible" | "restart_current_plan"
  checkAndRecover?: boolean
}

export type DesktopImportProgressStage =
  | "preflight"
  | "raw_asset"
  | "parser_initialization"
  | "document_ir"
  | "evidence"
  | "knowledge_analysis_plan"
  | "knowledge_analysis_batches"
  | "knowledge_analysis_merge"
  | "publication"

export type DesktopImportProgressStatus =
  | "pending"
  | "running"
  | "paused"
  | "cancelled"
  | "completed"
  | "failed"
  | "skipped"

export interface DesktopImportProgressStep {
  stage: DesktopImportProgressStage
  status: DesktopImportProgressStatus
  sourceStageRunId: string
  errorCode: string | null
  runtimeKind?: "parser" | "model"
  parserFamily?: "text" | "native_office" | "legacy_office" | "pdf"
  parserRoute?: "auto" | "plain_text" | "direct_structured" | "pymupdf_fast" | "bundled_onnx_ocr" | "tika_legacy"
  parserResourceState?: "resources_ready" | "unavailable"
  parserRuntimeState?: "not_loaded" | "initializing" | "ready" | "unavailable"
  completed?: number
  total?: number
}

export interface DesktopModelUsageAggregate {
  callCount: number
  attemptCount: number
  failureCount: number
  inputTokens: number
  outputTokens: number
  totalTokens: number
  totalCost: number | null
  tokenUsageSource: "provider_reported" | "estimated" | "mixed" | null
}

export interface DesktopModelUsageRecord {
  callId: string
  attempt: number
  attemptId: string
  operation: string
  modelRole: "default" | "analysis" | "answer"
  provider: string
  model: string
  jobId: string | null
  stageRunId: string | null
  batchId: string | null
  executionLane: "background" | "interactive"
  lifecycleStatus: string
  failureCode: string | null
  queueSeconds: number | null
  connectSeconds: number | null
  firstOutputSeconds: number | null
  totalSeconds: number | null
  inputTokens: number | null
  outputTokens: number | null
  totalTokens: number | null
  tokenUsageSource: "provider_reported" | "estimated" | null
  inputCost: number | null
  outputCost: number | null
  totalCost: number | null
  providerRequestId: string | null
  createdAt: string
  updatedAt: string
}

export interface DesktopModelActivity {
  operation: string
  modelRole: "default" | "analysis" | "answer"
  provider: string
  model: string
  callId: string
  attempt: number
  attemptId: string
  batchId: string | null
  executionLane: "background" | "interactive"
  status: "queued" | "connecting" | "awaiting_first_result" | "receiving_reasoning" | "receiving_output" | "validating" | "retrying" | "completed" | "interrupted" | "provider_failure" | "network_failure" | "model_result_failure"
  failureCode: string | null
  elapsedSeconds: number
  longWaitAdvisory: boolean
  longWaitThresholdSeconds: number
  availableActions: ("cancel" | "resume" | "retry")[]
}

export interface DesktopLegacyRecoveryChoice {
  allowed: boolean
  estimatedRemainingCalls: number
  estimatedInputTokens: number
  reusesCompletedBatches?: number
  reusesParserDocumentIrEvidence?: boolean
  discardedModelCheckpoints?: number
}

export interface DesktopLegacyModelRecovery {
  kind: "legacy_model_deadline" | "model_execution_profile_replan"
  compatible: boolean
  compatibilityReason: string
  previousPromptDigest: string | null
  provider: string | null
  model: string | null
  completedBatches: number
  totalBatches: number
  choices: Record<"continue_compatible" | "restart_current_plan", DesktopLegacyRecoveryChoice>
  recommendedChoice: "continue_compatible" | "restart_current_plan"
  selectedChoice: "continue_compatible" | "restart_current_plan" | null
  discardedModelCheckpoints: number
  startsAutomatically: false
}
