import type { DesktopModelUsageAggregate } from "./contracts-import-observability"

export type DesktopReasoningEffort = "off" | "low" | "medium" | "high"

/** Editable KB-local model role, capacity, reasoning, and user-priced cost values. */
export interface DesktopModelSettingsDraft {
  provider: string
  model: string
  apiBaseUrl: string
  apiKey: string
  maxConcurrentModelCalls: number
  analysisModel: string | null
  answerModel: string | null
  defaultContextCapacity: number | null
  analysisContextCapacity: number | null
  answerContextCapacity: number | null
  defaultReasoning: DesktopReasoningEffort | null
  analysisReasoning: DesktopReasoningEffort | null
  answerReasoning: DesktopReasoningEffort | null
  defaultInputPricePerMillion: number | null
  defaultOutputPricePerMillion: number | null
  analysisInputPricePerMillion: number | null
  analysisOutputPricePerMillion: number | null
  answerInputPricePerMillion: number | null
  answerOutputPricePerMillion: number | null
}

/** KB-local model values and sanitized aggregate usage returned by the Engine. */
export interface DesktopModelSettings extends DesktopModelSettingsDraft {
  apiKeyConfigured: boolean
  analysisConcurrency: number
  usageAggregate: DesktopModelUsageAggregate
}
