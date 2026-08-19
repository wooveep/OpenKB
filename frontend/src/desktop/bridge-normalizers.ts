import {
  DesktopBridgeError,
  type DesktopConversation,
  type DesktopConversationList,
  type DesktopGlobalSearchResults,
  type DesktopRuntimeLaunchIntent,
} from "./contracts"

let subscriptionSequence = 0

export function nextSubscriptionId(): string {
  subscriptionSequence += 1
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `desktop-subscription-${Date.now()}-${subscriptionSequence}`
}

export function toDesktopBridgeError(error: unknown): DesktopBridgeError {
  if (error instanceof DesktopBridgeError) return error
  if (error && typeof error === "object") {
    const candidate = error as { code?: unknown; message?: unknown }
    if (typeof candidate.code === "string" && typeof candidate.message === "string") {
      return new DesktopBridgeError(candidate.code, candidate.message)
    }
  }
  return new DesktopBridgeError(
    "desktop_bridge_failed",
    error instanceof Error ? error.message : String(error),
  )
}

export function runtimeLaunchIntents(payload: unknown): DesktopRuntimeLaunchIntent[] {
  if (!Array.isArray(payload)) return []
  return payload.filter(isRecord).flatMap((intent) => {
    const normalized = runtimeLaunchIntent(intent)
    return normalized === null ? [] : [normalized]
  })
}

function runtimeLaunchIntent(payload: Record<string, unknown>): DesktopRuntimeLaunchIntent | null {
  if (payload.kind === "openKnowledgeBase" && nonEmptyString(payload.kbDir)) {
    return { kind: "openKnowledgeBase", kbDir: payload.kbDir }
  }
  if (payload.kind === "importSources" && Array.isArray(payload.sourcePaths)) {
    const sourcePaths = payload.sourcePaths.filter(nonEmptyString)
    return sourcePaths.length ? { kind: "importSources", sourcePaths } : null
  }
  if (payload.kind === "previousKnowledgeBaseUnavailable" && nonEmptyString(payload.kbDir)) {
    return { kind: "previousKnowledgeBaseUnavailable", kbDir: payload.kbDir }
  }
  if (payload.kind === "activeKnowledgeBaseRestored") {
    return { kind: "activeKnowledgeBaseRestored" }
  }
  return null
}

export function conversationList(payload: unknown): DesktopConversationList {
  const value = record(payload)
  const items = Array.isArray(value.conversations) ? value.conversations : []
  return {
    conversations: items.map((item) => {
      const summary = record(item)
      return {
        conversationId: stringValue(summary, "conversation_id"),
        title: stringValue(summary, "title"),
        draftText: stringValue(summary, "draft_text"),
        createdAt: stringValue(summary, "created_at"),
        updatedAt: stringValue(summary, "updated_at"),
        generating: Boolean(summary.generating),
      }
    }),
    lastConversationId: nullableString(value.last_conversation_id),
  }
}

export function conversation(payload: unknown): DesktopConversation {
  const value = record(payload)
  return {
    conversationId: stringValue(value, "conversation_id"),
    title: stringValue(value, "title"),
    draftText: stringValue(value, "draft_text"),
    createdAt: stringValue(value, "created_at"),
    updatedAt: stringValue(value, "updated_at"),
    messages: (Array.isArray(value.messages) ? value.messages : []).map((item) => {
      const message = record(item)
      return {
        messageId: stringValue(message, "message_id"),
        ordinal: numberValue(message.ordinal),
        role: message.role === "assistant" ? "assistant" as const : "user" as const,
        content: stringValue(message, "content"),
        status: message.status === "generating" ? "generating" as const : message.status === "interrupted" ? "interrupted" as const : "completed" as const,
        selectedAnswerVersionId: nullableString(message.selected_answer_version_id),
        createdAt: stringValue(message, "created_at"),
        updatedAt: stringValue(message, "updated_at"),
        answerVersions: (Array.isArray(message.answer_versions) ? message.answer_versions : []).map(answerVersion),
      }
    }),
  }
}

export function globalSearchResults(payload: unknown, fallbackQuery: string): DesktopGlobalSearchResults {
  const value = record(payload)
  const items = Array.isArray(value.results) ? value.results : []
  return {
    query: typeof value.query === "string" ? value.query : fallbackQuery,
    results: items.map((item) => {
      const result = record(item)
      const kind = result.kind === "document" || result.kind === "knowledge_page"
        ? result.kind
        : "conversation"
      return {
        resultId: stringValue(result, "result_id"),
        kind,
        title: stringValue(result, "title"),
        snippet: stringValue(result, "snippet"),
        status: result.status === "failed" ? "failed" as const : "available" as const,
        documentId: nullableString(result.document_id),
        pageId: nullableString(result.page_id),
        conversationId: nullableString(result.conversation_id),
        messageId: nullableString(result.message_id),
      }
    }),
  }
}

function answerVersion(item: unknown): DesktopConversation["messages"][number]["answerVersions"][number] {
  const value = record(item)
  const plan = record(value.retrieval_plan)
  return {
    answerVersionId: stringValue(value, "answer_version_id"),
    versionNumber: numberValue(value.version_number),
    answerText: stringValue(value, "answer_text"),
    retrievalPlan: {
      query: stringValue(plan, "query"),
      terms: Array.isArray(plan.terms) ? plan.terms.filter((term): term is string => typeof term === "string") : [],
      source: stringValue(plan, "source"),
    },
    retrievalTrace: retrievalTrace(value.retrieval_trace),
    degradations: Array.isArray(value.degradations) ? value.degradations.filter((entry): entry is string => typeof entry === "string") : [],
    status: value.status === "interrupted" ? "interrupted" : "completed",
    interruptionCode: nullableString(value.interruption_code),
    interruptionReason: nullableString(value.interruption_reason),
    createdAt: stringValue(value, "created_at"),
    citations: (Array.isArray(value.citations) ? value.citations : []).map((item) => {
      const citation = record(item)
      return {
        evidenceId: stringValue(citation, "evidence_id"),
        documentId: stringValue(citation, "document_id"),
        documentName: stringValue(citation, "document_name"),
        section: stringValue(citation, "section"),
        locator: record(citation.locator),
        excerpt: stringValue(citation, "excerpt"),
        channels: Array.isArray(citation.channels) ? citation.channels.filter((channel): channel is string => typeof channel === "string") : [],
        sourceAvailable: Boolean(citation.source_available),
      }
    }),
    sourceImages: (Array.isArray(value.source_images) ? value.source_images : []).map((item) => {
      const image = record(item)
      return {
        sourceImageId: stringValue(image, "source_image_id"),
        evidenceId: stringValue(image, "evidence_id"),
        documentId: stringValue(image, "document_id"),
        documentName: stringValue(image, "document_name"),
        name: stringValue(image, "name"),
        mediaType: stringValue(image, "media_type"),
        filePath: stringValue(image, "file_path"),
        altText: nullableString(image.alt_text),
        locator: record(image.locator),
        sourceAvailable: Boolean(image.source_available),
      }
    }),
  }
}

function retrievalTrace(payload: unknown): DesktopConversation["messages"][number]["answerVersions"][number]["retrievalTrace"] {
  const value = record(payload)
  const strings = (item: unknown) => Array.isArray(item)
    ? item.filter((entry): entry is string => typeof entry === "string")
    : []
  return {
    catalogGenerationIds: strings(value.catalog_generation_ids),
    pageTreeGenerationIds: strings(value.page_tree_generation_ids),
    channels: (Array.isArray(value.channels) ? value.channels : []).map((item) => {
      const channel = record(item)
      return {
        channel: stringValue(channel, "channel"),
        candidateCount: numberValue(channel.candidate_count),
        triggerReasons: strings(channel.trigger_reasons),
        degradationReasons: strings(channel.degradation_reasons),
      }
    }),
    triggerReasons: strings(value.trigger_reasons),
    degradationReasons: strings(value.degradation_reasons),
    selectedNodeIds: strings(value.selected_node_ids),
    canonicalEvidenceIds: strings(value.canonical_evidence_ids),
    fusionPolicyVersion: stringValue(value, "fusion_policy_version"),
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0
}

function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {}
}

function stringValue(value: Record<string, unknown>, key: string): string {
  return typeof value[key] === "string" ? value[key] : ""
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null
}

function numberValue(value: unknown): number {
  return typeof value === "number" ? value : 0
}
