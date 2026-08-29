import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import React from "react"
import { renderToStaticMarkup } from "react-dom/server"
import i18next from "i18next"
import { I18nextProvider, initReactI18next } from "react-i18next"
import react from "@vitejs/plugin-react"
import { createServer } from "vite"

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const translations = JSON.parse(
  await readFile(resolve(frontendDir, "src/locales/en/common.json"), "utf8"),
)
const failedDocumentsSource = await readFile(
  resolve(frontendDir, "src/desktop/FailedDocumentsDialog.tsx"),
  "utf8",
)
const knowledgeWorkspaceSource = await readFile(
  resolve(frontendDir, "src/desktop/DesktopKnowledgeWorkspacePanel.tsx"),
  "utf8",
)
const knowledgePageSource = await readFile(
  resolve(frontendDir, "src/desktop/DesktopKnowledgePagePanel.tsx"),
  "utf8",
)
const mutationReloadSource = knowledgeWorkspaceSource.slice(
  knowledgeWorkspaceSource.indexOf("const reloadAfterUserMutation"),
  knowledgeWorkspaceSource.indexOf("useEffect(() =>", knowledgeWorkspaceSource.indexOf("const reloadAfterUserMutation")),
)
for (const expected of [
  "checkAndRecover: true",
  "reasoning:",
  "discardedModelCheckpoints",
  "recoveryProfileReplanNotice",
  "checkAndRecover",
]) {
  assert.match(failedDocumentsSource, new RegExp(expected))
}
assert.match(knowledgeWorkspaceSource, /currentRequestSequence/)
assert.match(knowledgeWorkspaceSource, /knowledgeWorkspaceRequestIsCurrent/)
assert.match(
  knowledgeWorkspaceSource,
  /!disposed && knowledgeWorkspaceRequestIsCurrent/,
)
assert.match(knowledgeWorkspaceSource, /"create_new"/)
assert.match(knowledgeWorkspaceSource, /"use_existing"/)
assert.match(knowledgeWorkspaceSource, /onKnowledgePagesChanged=\{reloadAfterUserMutation\}/)
assert.match(knowledgeWorkspaceSource, /queryRef\.current/)
assert.match(knowledgeWorkspaceSource, /pendingPreferredPageIdRef/)
assert.match(knowledgeWorkspaceSource, /requestSequence,\s*currentRequestSequence\.current/)
assert.match(knowledgeWorkspaceSource, /loadCurrent\("", pageId, requestSequence\)/)
assert.match(knowledgeWorkspaceSource, /candidateUnavailable/)
assert.match(knowledgeWorkspaceSource, /embedded/)
assert.match(mutationReloadSource, /\.finally\(\(\) =>/)
assert.match(mutationReloadSource, /setLoading\(false\)/)
assert.match(knowledgePageSource, /onKnowledgePagesChangedRef\.current/)
assert.match(knowledgePageSource, /mountedRef\.current \? preferredPageId : null/)
assert.match(knowledgePageSource, /notifyKnowledgePagesChanged/)
assert.match(knowledgePageSource, /!embedded \? \(/)
await i18next.use(initReactI18next).init({
  lng: "en",
  fallbackLng: "en",
  resources: { en: { common: translations } },
  interpolation: { escapeValue: false },
})

const vite = await createServer({
  configFile: false,
  root: frontendDir,
  appType: "custom",
  plugins: [react()],
  resolve: { alias: { "@": resolve(frontendDir, "src") } },
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true },
})

try {
  const { DesktopImportProgress } = await vite.ssrLoadModule(
    "/src/desktop/DesktopImportProgress.tsx",
  )
  const { DesktopKnowledgeGraphExtractionTasks } = await vite.ssrLoadModule(
    "/src/desktop/DesktopKnowledgeGraphExtractionTasks.tsx",
  )
  const { EffectiveModelRoleSettings } = await vite.ssrLoadModule(
    "/src/desktop/DesktopModelSettingsPanel.tsx",
  )
  const { DesktopModelResultDetails } = await vite.ssrLoadModule(
    "/src/desktop/DesktopModelResultDetails.tsx",
  )
  const { DesktopCapabilityDegradationNotice } = await vite.ssrLoadModule(
    "/src/desktop/DesktopCapabilityDegradationNotice.tsx",
  )
  const { runDocumentImportBatch } = await vite.ssrLoadModule(
    "/src/desktop/desktop-import-batch.ts",
  )
  const { createLatestRefresh } = await vite.ssrLoadModule(
    "/src/desktop/latest-refresh.ts",
  )
  const { TauriKnowledgePageBridge } = await vite.ssrLoadModule(
    "/src/desktop/tauri-knowledge-page-bridge.ts",
  )
  const {
    knowledgeWorkspaceRequestIsCurrent,
    reloadKnowledgeWorkspaceAfterUserMutation,
  } = await vite.ssrLoadModule(
    "/src/desktop/knowledge-workspace-refresh.ts",
  )
  const mutationReloads = []
  await reloadKnowledgeWorkspaceAfterUserMutation(
    async (search, preferredPageId, requestSequence) => {
      mutationReloads.push([search, preferredPageId, requestSequence])
      return "loaded"
    },
    "atlas",
    "page-1",
    11,
  )
  assert.deepEqual(mutationReloads, [["atlas", "page-1", 11]])
  mutationReloads.length = 0
  await reloadKnowledgeWorkspaceAfterUserMutation(
    async (search, preferredPageId, requestSequence) => {
      mutationReloads.push([search, preferredPageId, requestSequence])
      return mutationReloads.length > 1 ? "loaded" : "preferred_missing"
    },
    "atlas",
    "page-deleted-or-filtered",
    12,
  )
  assert.deepEqual(mutationReloads, [
    ["atlas", "page-deleted-or-filtered", 12],
    ["atlas", undefined, 12],
  ])
  mutationReloads.length = 0
  await reloadKnowledgeWorkspaceAfterUserMutation(
    async (search, preferredPageId, requestSequence) => {
      mutationReloads.push([search, preferredPageId, requestSequence])
      return "loaded"
    },
    "atlas",
    null,
    13,
  )
  assert.deepEqual(mutationReloads, [["atlas", undefined, 13]])
  mutationReloads.length = 0
  let activeSequence = 14
  let releaseStaleReload
  const staleReloadGate = new Promise((resolveGate) => { releaseStaleReload = resolveGate })
  const staleReload = reloadKnowledgeWorkspaceAfterUserMutation(
    async (search, preferredPageId, requestSequence) => {
      mutationReloads.push([search, preferredPageId, requestSequence])
      await staleReloadGate
      return requestSequence === activeSequence ? "preferred_missing" : "stale"
    },
    "old-query",
    "page-a",
    activeSequence,
  )
  activeSequence += 1
  releaseStaleReload()
  await staleReload
  assert.deepEqual(mutationReloads, [["old-query", "page-a", 14]])
  const staleErrors = []
  let activeErrorSequence = 20
  let rejectOldRequest
  const oldRequest = new Promise((_resolveRequest, rejectRequest) => {
    rejectOldRequest = rejectRequest
  }).catch((reason) => {
    if (knowledgeWorkspaceRequestIsCurrent(20, activeErrorSequence)) {
      staleErrors.push(reason.message)
    }
  })
  activeErrorSequence += 1
  rejectOldRequest(new Error("old request failed"))
  await oldRequest
  assert.deepEqual(staleErrors, [])
  const knowledgeCalls = []
  class CapturingKnowledgePageBridge extends TauriKnowledgePageBridge {
    call(command, args) {
      knowledgeCalls.push({ command, args })
      return Promise.resolve({})
    }
  }
  const capturingKnowledgeBridge = new CapturingKnowledgePageBridge()
  await capturingKnowledgeBridge.getKnowledgeWorkspaceItem({
    authority: "generated",
    generationId: 7,
    itemKey: "item-1",
  })
  assert.deepEqual(knowledgeCalls, [{
    command: "desktop_get_knowledge_workspace_item",
    args: {
      item: { authority: "generated", generationId: 7, itemKey: "item-1" },
    },
  }])
  const activity = {
    operation: "knowledge_graph_extraction",
    modelRole: "analysis",
    provider: "custom",
    model: "analysis-model",
    callId: "graph-call-1",
    attempt: 1,
    attemptId: "graph-call-1:1",
    batchId: null,
    executionLane: "background",
    status: "receiving_output",
    failureCode: null,
    elapsedSeconds: 367,
    longWaitAdvisory: true,
    longWaitThresholdSeconds: 300,
    availableActions: ["cancel"],
  }
  const progressMarkup = render(
    React.createElement(DesktopImportProgress, {
      steps: [],
      activity,
      usage: {
        callCount: 1,
        attemptCount: 1,
        failureCount: 0,
        inputTokens: 120,
        outputTokens: 30,
        totalTokens: 150,
        totalCost: null,
        tokenUsageSource: "estimated",
      },
      records: [],
    }),
  )
  for (const expected of [
    "knowledge_graph_extraction",
    "analysis-model",
    "graph-call-1",
    "graph-call-1:1",
    "Receiving model output",
    "estimated",
    "still running and has not timed out",
  ]) {
    assert.match(progressMarkup, new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))
  }

  const graphMarkup = render(
    React.createElement(DesktopKnowledgeGraphExtractionTasks, {
      bridge: {},
      tasks: [
        {
          documentId: "document-1",
          documentName: "guide.docx",
          status: "pending",
          reason: "initial",
          provider: "custom",
          model: "analysis-model",
          attemptCount: 1,
          modelAttempt: 1,
          callId: "graph-call-1",
          errorCode: null,
          errorReason: null,
          updatedAt: "2026-08-23T00:00:00Z",
          completedAt: null,
          modelActivity: null,
        },
      ],
    }),
  )
  assert.match(graphMarkup, /Knowledge Graph extraction/)
  assert.match(graphMarkup, /guide\.docx/)
  assert.match(graphMarkup, /graph-call-1:1/)
  assert.match(graphMarkup, /Resume extraction/)
  const emptyGraphMarkup = render(
    React.createElement(DesktopKnowledgeGraphExtractionTasks, {
      bridge: {},
      tasks: [{
        documentId: "document-empty", documentName: "empty.md",
        status: "completed_empty", nodeCount: 0, edgeCount: 0, reason: "initial",
        provider: "custom", model: "analysis-model", attemptCount: 1, modelAttempt: 1,
        callId: "graph-call-empty", errorCode: null, errorReason: null,
        updatedAt: "2026-08-28T00:00:00Z", completedAt: "2026-08-28T00:00:00Z",
        modelActivity: null,
      }],
    }),
  )
  assert.match(emptyGraphMarkup, /Completed \(no supported relations\)/)
  assert.match(emptyGraphMarkup, /0 nodes · 0 edges/)
  const degradedGraphMarkup = render(
    React.createElement(DesktopKnowledgeGraphExtractionTasks, {
      bridge: {},
      tasks: [{
        documentId: "document-degraded", documentName: "partial.md",
        status: "completed", nodeCount: 2, edgeCount: 1, quality: "degraded",
        retainedCount: 3, weakenedCount: 1, rejectedCount: 2, reason: "initial",
        provider: "deepseek", model: "deepseek-chat", attemptCount: 1, modelAttempt: 1,
        callId: "graph-call-degraded", errorCode: null, errorReason: null,
        updatedAt: "2026-08-29T00:00:00Z", completedAt: "2026-08-29T00:00:00Z",
        modelActivity: null,
      }],
    }),
  )
  assert.match(degradedGraphMarkup, /Degraded/)
  assert.match(degradedGraphMarkup, /3 retained · 1 weakened · 2 rejected/)
  assert.match(degradedGraphMarkup, /Retry extraction/)
  const degradationMarkup = render(
    React.createElement(DesktopCapabilityDegradationNotice, {
      codes: [
        "retrieval_plan_unverified",
        "retrieval_plan_unavailable",
        "retrieval_plan_cancelled",
        "page_tree_selection_unavailable",
        "page_tree_selection_cancelled",
        "answer_model_unavailable",
        "answer_model_fallback",
      ],
      onOpenModelSettings: () => undefined,
      onRetry: () => undefined,
    }),
  )
  assert.match(degradationMarkup, /Retrieval planning is unverified/)
  assert.match(degradationMarkup, /Retrieval planning is not configured/)
  assert.match(degradationMarkup, /Retrieval planning was cancelled/)
  assert.match(degradationMarkup, /Page-tree selection is not configured/)
  assert.match(degradationMarkup, /Page-tree selection was cancelled/)
  assert.match(degradationMarkup, /Answer model is not configured/)
  assert.match(degradationMarkup, /Answer model used a fallback/)
  assert.match(degradationMarkup, /Use Save and Verify/)
  assert.match(degradationMarkup, /Open Model Settings/)
  assert.match(degradationMarkup, /Retry this answer/)
  assert.match(degradationMarkup, /Technical details/)
  const fallbackDegradationMarkup = render(
    React.createElement(DesktopCapabilityDegradationNotice, {
      codes: ["retrieval_plan_fallback"],
    }),
  )
  assert.match(fallbackDegradationMarkup, /Retrieval planning is suspended/)
  assert.doesNotMatch(fallbackDegradationMarkup, /Save and Verify/)
  const effectiveSettingsMarkup = render(
    React.createElement(EffectiveModelRoleSettings, {
      adapter: {
        identity: "deepseek",
        version: "deepseek.v1",
        structuredOutputMode: "json_object",
        supportsStructuredAnalysis: true,
        supportedReasoning: ["high", "low", "medium", "off"],
        analysisUnavailableReason: null,
      },
      roles: {
        default: { model: "deepseek-v4-pro", contextCapacity: 64000, reasoning: null, reasoningSource: "provider_default" },
        analysis: { model: "deepseek-v4-pro", contextCapacity: 64000, reasoning: "off", reasoningSource: "analysis_safe_default" },
        answer: { model: "deepseek-v4-pro", contextCapacity: 64000, reasoning: null, reasoningSource: "provider_default" },
      },
      analysisCapability: {
        profileIdentity: "profile-1",
        status: "verified",
        failureCode: null,
        reason: null,
        checkedAt: "2026-08-26T00:00:00+00:00",
      },
      answerCapability: {
        profileIdentity: "answer-profile-1",
        status: "unchecked",
        failureCode: null,
        reason: null,
        checkedAt: null,
      },
    }),
  )
  for (const expected of ["DeepSeek", "json_object", "Analysis", "Answer", "Off", "Provider default", "Verified", "Unchecked"]) {
    assert.match(effectiveSettingsMarkup, new RegExp(expected))
  }
  const resultDetailsMarkup = render(
    React.createElement(DesktopModelResultDetails, {
      result: {
        finishReason: "length",
        reasoningObserved: true,
        finalContentObserved: false,
        reasoningChunkCount: 3,
        finalChunkCount: 0,
        reasoningCharacterCount: 240,
        finalCharacterCount: 0,
        inputTokens: 10,
        outputTokens: 90,
        totalTokens: 100,
        providerRequestId: "safe-request-id",
      },
    }),
  )
  for (const expected of ["Model result details", "length", "3", "240", "100", "safe-request-id"]) {
    assert.match(resultDetailsMarkup, new RegExp(expected))
  }
  let activeImports = 0
  let peakImports = 0
  await runDocumentImportBatch([0, 1, 2, 3, 4, 5], async () => {
    activeImports += 1
    peakImports = Math.max(peakImports, activeImports)
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 5))
    activeImports -= 1
  })
  assert.equal(peakImports, 4)

  let activeRefreshLoads = 0
  let peakRefreshLoads = 0
  let refreshLoadNumber = 0
  const releaseRefreshLoads = []
  const committedRefreshes = []
  const refresh = createLatestRefresh({
    load: () => {
      refreshLoadNumber += 1
      activeRefreshLoads += 1
      peakRefreshLoads = Math.max(peakRefreshLoads, activeRefreshLoads)
      return new Promise((resolveLoad) => {
        releaseRefreshLoads.push(() => {
          activeRefreshLoads -= 1
          resolveLoad(refreshLoadNumber)
        })
      })
    },
    commit: (value) => committedRefreshes.push(value),
  })
  refresh.request()
  refresh.request()
  refresh.request()
  assert.equal(refreshLoadNumber, 1)
  assert.equal(peakRefreshLoads, 1)
  releaseRefreshLoads.shift()()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(refreshLoadNumber, 2)
  assert.deepEqual(committedRefreshes, [])
  releaseRefreshLoads.shift()()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(peakRefreshLoads, 1)
  assert.deepEqual(committedRefreshes, [2])
  refresh.dispose()
  refresh.request()
  assert.equal(refreshLoadNumber, 2)
  console.log("model task UI tests: OK")
} finally {
  await vite.close()
}

function render(element) {
  return renderToStaticMarkup(
    React.createElement(I18nextProvider, { i18n: i18next }, element),
  )
}
