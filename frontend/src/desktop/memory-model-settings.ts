import type {
  DesktopModelConnectionTest,
  DesktopModelSettings,
  DesktopModelSettingsDraft,
  DesktopSaveAndVerifyModelConfiguration,
} from "./contracts"
import { MemoryKnowledgeReviewBridge } from "./memory-knowledge-review-bridge"

/** Fresh model-settings state for one isolated in-memory Desktop Bridge. */
export function createMemoryModelSettings(): DesktopModelSettings {
  return {
    provider: "custom",
    model: "gpt-5.4",
    apiBaseUrl: "https://api.openai.com/v1",
    apiKey: "",
    apiKeyConfigured: false,
    maxConcurrentModelCalls: 1,
    requestsPerMinute: null,
    tokensPerMinute: null,
    analysisModel: null,
    answerModel: null,
    defaultContextCapacity: null,
    analysisContextCapacity: null,
    answerContextCapacity: null,
    defaultReasoning: null,
    analysisReasoning: null,
    answerReasoning: null,
    defaultInputPricePerMillion: null,
    defaultOutputPricePerMillion: null,
    analysisInputPricePerMillion: null,
    analysisOutputPricePerMillion: null,
    answerInputPricePerMillion: null,
    answerOutputPricePerMillion: null,
    analysisConcurrency: 1,
    providerAdapter: {
      identity: "custom",
      version: "custom.compatibility.v1",
      structuredOutputMode: null,
      supportsStructuredAnalysis: false,
      supportedReasoning: [],
      analysisUnavailableReason: "Custom providers do not have a code-owned structured Analysis protocol.",
    },
    effectiveRoles: {
      default: { model: "gpt-5.4", contextCapacity: 128000, reasoning: null, reasoningSource: "provider_default" },
      analysis: { model: "gpt-5.4", contextCapacity: 128000, reasoning: "off", reasoningSource: "analysis_safe_default" },
      answer: { model: "gpt-5.4", contextCapacity: 128000, reasoning: null, reasoningSource: "provider_default" },
    },
    analysisCapability: {
      profileIdentity: null,
      status: "unchecked",
      failureCode: "analysis_profile_unavailable",
      reason: "Custom providers do not have a code-owned structured Analysis protocol.",
      checkedAt: null,
    },
    answerCapability: {
      profileIdentity: null,
      status: "unchecked",
      failureCode: null,
      reason: null,
      checkedAt: null,
    },
    usageAggregate: {
      callCount: 0,
      attemptCount: 0,
      failureCount: 0,
      inputTokens: 0,
      outputTokens: 0,
      totalTokens: 0,
      totalCost: null,
      tokenUsageSource: null,
    },
  }
}

/** Model-settings behavior shared by the renderer-only Desktop Bridge. */
export abstract class MemoryModelSettingsBridge extends MemoryKnowledgeReviewBridge {
  private modelSettingsResult: DesktopModelSettings = createMemoryModelSettings()

  async modelSettings(): Promise<DesktopModelSettings> {
    return this.modelSettingsResult
  }

  async saveModelSettings(
    settings: DesktopModelSettingsDraft,
    requestId: string,
  ): Promise<DesktopModelSettings> {
    void requestId
    this.modelSettingsResult = {
      ...this.modelSettingsResult,
      ...settings,
      apiKeyConfigured: Boolean(settings.apiKey),
      analysisConcurrency: settings.maxConcurrentModelCalls,
    }
    return this.modelSettingsResult
  }

  async saveAndVerifyModelSettings(
    settings: DesktopModelSettingsDraft,
    requestId: string,
  ): Promise<DesktopSaveAndVerifyModelConfiguration> {
    const saved = await this.saveModelSettings(settings, requestId)
    const verification = await this.testModelConnection(settings, requestId)
    return {
      saved: true,
      verificationCostAccepted: true,
      allRequiredRolesVerified:
        verification.roleResults.analysis.status === "verified"
        && verification.roleResults.answer.status === "verified",
      cancelled: false,
      models: verification.models,
      attemptCount: verification.attemptCount,
      latencyMs: verification.latencyMs,
      roleResults: verification.roleResults,
      settings: saved,
    }
  }

  async testModelConnection(
    settings: DesktopModelSettingsDraft,
    requestId: string,
  ): Promise<DesktopModelConnectionTest> {
    void requestId
    return {
      ok: true,
      model: settings.model,
      models: [settings.model],
      latencyMs: 42,
      attemptCount: 1,
      profileIdentity: "memory-answer-profile",
      capabilityStatus: "answer_verified",
      roleResults: {
        default: { role: "default", model: settings.model, status: "verified", reason: null, attemptCount: 1, profileIdentity: "memory-answer-profile", cached: false, coveredBy: "answer" },
        analysis: {
          role: "analysis",
          model: null,
          status: "unavailable",
          reason: "The memory Custom provider has no structured Analysis adapter.",
          attemptCount: 0,
          profileIdentity: null,
          cached: false,
          coveredBy: null,
        },
        answer: {
          role: "answer",
          model: settings.model,
          status: "verified",
          reason: null,
          attemptCount: 1,
          profileIdentity: "memory-answer-profile",
          cached: false,
          coveredBy: null,
        },
      },
    }
  }
}
