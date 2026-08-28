import type { DesktopModelUsageAggregate } from "./contracts-import-observability"

export type DesktopReasoningEffort = "off" | "low" | "medium" | "high"
export type DesktopStructuredOutputMode = "json_schema" | "json_object" | "prompt_contract"
export type DesktopReasoningSource =
  | "explicit_role"
  | "inherited_default"
  | "analysis_safe_default"
  | "provider_default"

export interface DesktopModelProviderAdapter {
  identity: string
  version: string
  structuredOutputMode: DesktopStructuredOutputMode | null
  supportsStructuredAnalysis: boolean
  supportedReasoning: DesktopReasoningEffort[]
  analysisUnavailableReason: string | null
}

export interface DesktopEffectiveModelRole {
  model: string
  contextCapacity: number
  reasoning: DesktopReasoningEffort | null
  reasoningSource: DesktopReasoningSource
}

export interface DesktopEffectiveModelRoles {
  default: DesktopEffectiveModelRole
  analysis: DesktopEffectiveModelRole
  answer: DesktopEffectiveModelRole
}

export type DesktopModelCapabilityStatus =
  | "unchecked"
  | "checking"
  | "verified"
  | "failed"
  | "cancelled"

export interface DesktopModelCapabilityState {
  profileIdentity: string | null
  status: DesktopModelCapabilityStatus
  failureCode: string | null
  reason: string | null
  checkedAt: string | null
}

export type DesktopModelCapabilityCheckStatus = "verified" | "answer_verified"
export type DesktopModelCapabilityCheckRoleStatus =
  | "verified"
  | "unavailable"
  | "failed"
  | "cancelled"
  | "unverified"
  | "not_required"
export type DesktopModelCapabilityCheckRole = "default" | "analysis" | "answer"

export interface DesktopModelCapabilityCheckRoleResult {
  role: DesktopModelCapabilityCheckRole
  model: string | null
  status: DesktopModelCapabilityCheckRoleStatus
  reason: string | null
  failureCode?: string | null
  attemptCount: number
  profileIdentity: string | null
  cached: boolean
  coveredBy: DesktopModelCapabilityCheckRole | null
}

export interface DesktopModelCapabilityCheckRoleResults {
  default: DesktopModelCapabilityCheckRoleResult
  analysis: DesktopModelCapabilityCheckRoleResult
  answer: DesktopModelCapabilityCheckRoleResult
}

export interface DesktopModelConnectionTest {
  ok: boolean
  model: string
  models: string[]
  latencyMs: number
  attemptCount: number
  profileIdentity: string
  capabilityStatus: DesktopModelCapabilityCheckStatus
  roleResults: DesktopModelCapabilityCheckRoleResults
}

/** Persisted configuration plus independent, cost-consented checks for each required role. */
export interface DesktopSaveAndVerifyModelConfiguration {
  saved: boolean
  verificationCostAccepted: boolean
  allRequiredRolesVerified: boolean
  cancelled: boolean
  models: string[]
  attemptCount: number
  latencyMs: number
  roleResults: DesktopModelCapabilityCheckRoleResults
  settings: DesktopModelSettings
}

/** Editable KB-local model role, capacity, reasoning, and user-priced cost values. */
export interface DesktopModelSettingsDraft {
  provider: string
  model: string
  apiBaseUrl: string
  apiKey: string
  maxConcurrentModelCalls: number
  requestsPerMinute: number | null
  tokensPerMinute: number | null
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
  providerAdapter: DesktopModelProviderAdapter
  effectiveRoles: DesktopEffectiveModelRoles
  analysisCapability: DesktopModelCapabilityState
  answerCapability: DesktopModelCapabilityState
  usageAggregate: DesktopModelUsageAggregate
}
