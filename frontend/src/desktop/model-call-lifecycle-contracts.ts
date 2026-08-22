/** Content-free Model Call lifecycle values forwarded through the Desktop Bridge. */

export type DesktopModelCallLifecycleStatus =
  | "queued"
  | "connecting"
  | "awaiting_model_result"
  | "model_output_activity"
  | "completed"
  | "retrying"
  | "cancelled"
  | "provider_failure"
  | "network_failure"

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
  }
}
