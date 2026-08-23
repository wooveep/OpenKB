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
  console.log("model task UI tests: OK")
} finally {
  await vite.close()
}

function render(element) {
  return renderToStaticMarkup(
    React.createElement(I18nextProvider, { i18n: i18next }, element),
  )
}
