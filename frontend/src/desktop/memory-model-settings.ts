import type { DesktopModelSettings } from "./contracts"

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
