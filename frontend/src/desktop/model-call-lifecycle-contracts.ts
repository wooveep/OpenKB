/** Content-free Model Call lifecycle values forwarded through the Desktop Bridge. */

export type DesktopModelCallLifecycleStatus =
  | "queued"
  | "connecting"
  | "awaiting_model_result"
  | "reasoning_output_activity"
  | "model_output_activity"
  | "validating"
  | "completed"
  | "retrying"
  | "cancelled"
  | "provider_failure"
  | "network_failure"
  | "model_result_failure"

export interface DesktopModelCallLifecycleEvent {
  sequence: number
  kind: "model.call_lifecycle"
  data: {
    requestId: string
    callId: string
    attempt: number
    status: DesktopModelCallLifecycleStatus
    elapsedSeconds: number
    failureCode: string | null
    reason: string | null
    retryAfterSeconds: number | null
    operation: string
    modelRole: "default" | "analysis" | "answer"
    provider: string
    modelName: string
    executionLane: "background" | "interactive"
    attemptId: string
    longWaitThresholdSeconds: number
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
}
