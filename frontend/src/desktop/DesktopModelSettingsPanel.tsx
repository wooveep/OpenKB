import { CheckCircle2, Download, Eye, EyeOff, FolderOpen, KeyRound, Loader2, Save, SlidersHorizontal, Square } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { useLanguage } from "@/lib/language-context"
import { useTheme } from "@/lib/theme-context"
import { useZoom } from "@/lib/zoom"
import { useDesktopBridge } from "./bridge-context"
import { nextDesktopRequestId } from "./request-id"
import {
  DesktopBridgeError,
  type DesktopModelCallLifecycleStatus,
  type DesktopModelSettingsDraft as SettingsDraft,
  type DesktopModelSettings,
  type DesktopReasoningEffort,
} from "./contracts"

type ModelSettingsDraft = {
  provider: string
  model: string
  apiBaseUrl: string
  apiKey: string
  maxConcurrentModelCalls: string
  analysisModel: string
  answerModel: string
  defaultContextCapacity: string
  analysisContextCapacity: string
  answerContextCapacity: string
  defaultReasoning: "" | DesktopReasoningEffort
  analysisReasoning: "" | DesktopReasoningEffort
  answerReasoning: "" | DesktopReasoningEffort
  defaultInputPricePerMillion: string
  defaultOutputPricePerMillion: string
  analysisInputPricePerMillion: string
  analysisOutputPricePerMillion: string
  answerInputPricePerMillion: string
  answerOutputPricePerMillion: string
}

const diagnosticFiles = [
  "manifest.json",
  "model-settings.json",
  "import-jobs.json",
  "model-calls.json",
  "model-usage.json",
  "graph-diagnostics.json",
  "page-tree-enrichment.json",
  "integrity.json",
]

const roleFields = [
  { role: "default", model: "model", context: "defaultContextCapacity", reasoning: "defaultReasoning", inputPrice: "defaultInputPricePerMillion", outputPrice: "defaultOutputPricePerMillion" },
  { role: "analysis", model: "analysisModel", context: "analysisContextCapacity", reasoning: "analysisReasoning", inputPrice: "analysisInputPricePerMillion", outputPrice: "analysisOutputPricePerMillion" },
  { role: "answer", model: "answerModel", context: "answerContextCapacity", reasoning: "answerReasoning", inputPrice: "answerInputPricePerMillion", outputPrice: "answerOutputPricePerMillion" },
] as const

const reasoningOptions = ["", "off", "low", "medium", "high"] as const

function draftFrom(settings: DesktopModelSettings): ModelSettingsDraft {
  return {
    provider: settings.provider,
    model: settings.model,
    apiBaseUrl: settings.apiBaseUrl,
    apiKey: settings.apiKey,
    maxConcurrentModelCalls: String(settings.maxConcurrentModelCalls),
    analysisModel: settings.analysisModel ?? "",
    answerModel: settings.answerModel ?? "",
    defaultContextCapacity: settings.defaultContextCapacity?.toString() ?? "",
    analysisContextCapacity: settings.analysisContextCapacity?.toString() ?? "",
    answerContextCapacity: settings.answerContextCapacity?.toString() ?? "",
    defaultReasoning: settings.defaultReasoning ?? "",
    analysisReasoning: settings.analysisReasoning ?? "",
    answerReasoning: settings.answerReasoning ?? "",
    defaultInputPricePerMillion: settings.defaultInputPricePerMillion?.toString() ?? "",
    defaultOutputPricePerMillion: settings.defaultOutputPricePerMillion?.toString() ?? "",
    analysisInputPricePerMillion: settings.analysisInputPricePerMillion?.toString() ?? "",
    analysisOutputPricePerMillion: settings.analysisOutputPricePerMillion?.toString() ?? "",
    answerInputPricePerMillion: settings.answerInputPricePerMillion?.toString() ?? "",
    answerOutputPricePerMillion: settings.answerOutputPricePerMillion?.toString() ?? "",
  }
}

type DraftValidation =
  | { settings: SettingsDraft; error: null }
  | { settings: null; error: "invalidConcurrency" | "invalidContextCapacity" | "invalidPrice" }

function validatedDraft(draft: ModelSettingsDraft): DraftValidation {
  const maxConcurrentModelCalls = Number(draft.maxConcurrentModelCalls)
  if (!Number.isInteger(maxConcurrentModelCalls) || maxConcurrentModelCalls < 1 || maxConcurrentModelCalls > 4) {
    return { settings: null, error: "invalidConcurrency" }
  }
  const capacityValues = [
    draft.defaultContextCapacity,
    draft.analysisContextCapacity,
    draft.answerContextCapacity,
  ]
  const capacities = capacityValues.map((value) => value.trim() ? Number(value) : null)
  if (capacities.some((value) => value !== null && (!Number.isInteger(value) || value < 4096))) {
    return { settings: null, error: "invalidContextCapacity" }
  }
  const priceValues = [
    draft.defaultInputPricePerMillion,
    draft.defaultOutputPricePerMillion,
    draft.analysisInputPricePerMillion,
    draft.analysisOutputPricePerMillion,
    draft.answerInputPricePerMillion,
    draft.answerOutputPricePerMillion,
  ]
  const prices = priceValues.map((value) => value.trim() ? Number(value) : null)
  if (prices.some((value) => value !== null && (!Number.isFinite(value) || value < 0))) {
    return { settings: null, error: "invalidPrice" }
  }
  return {
    error: null,
    settings: {
      provider: draft.provider,
      model: draft.model.trim(),
      apiBaseUrl: draft.apiBaseUrl.trim(),
      apiKey: draft.apiKey.trim(),
      maxConcurrentModelCalls,
      analysisModel: draft.analysisModel.trim() || null,
      answerModel: draft.answerModel.trim() || null,
      defaultContextCapacity: capacities[0],
      analysisContextCapacity: capacities[1],
      answerContextCapacity: capacities[2],
      defaultReasoning: draft.defaultReasoning || null,
      analysisReasoning: draft.analysisReasoning || null,
      answerReasoning: draft.answerReasoning || null,
      defaultInputPricePerMillion: prices[0],
      defaultOutputPricePerMillion: prices[1],
      analysisInputPricePerMillion: prices[2],
      analysisOutputPricePerMillion: prices[3],
      answerInputPricePerMillion: prices[4],
      answerOutputPricePerMillion: prices[5],
    },
  }
}

/** Model connection defaults and opt-in support diagnostics for the active knowledge base. */
export function DesktopModelSettingsPanel({ kbDir }: { kbDir: string }) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const { language, setLanguage } = useLanguage()
  const { theme, setTheme } = useTheme()
  const { zoom, setZoom } = useZoom()
  const [settings, setSettings] = useState<DesktopModelSettings | null>(null)
  const [draft, setDraft] = useState<ModelSettingsDraft | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testStatus, setTestStatus] = useState<DesktopModelCallLifecycleStatus | null>(null)
  const [testResult, setTestResult] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [diagnosticReviewOpen, setDiagnosticReviewOpen] = useState(false)
  const [showApiKey, setShowApiKey] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const testRequestIdRef = useRef<string | null>(null)

  useEffect(() => {
    let disposed = false
    void bridge.modelSettings()
      .then((result) => {
        if (disposed) return
        setSettings(result)
        setDraft(draftFrom(result))
      })
      .catch((reason) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!disposed) setLoading(false)
      })
    return () => { disposed = true }
  }, [bridge])

  useEffect(() => {
    let disposed = false
    let unsubscribe: (() => void) | undefined
    void bridge.subscribe((event) => {
      if (
        event.kind === "model.call_lifecycle"
        && event.data.requestId === testRequestIdRef.current
      ) {
        setTestStatus(event.data.status)
      }
    }).then((remove) => {
      if (disposed) remove()
      else unsubscribe = remove
    }).catch(() => undefined)
    return () => {
      disposed = true
      unsubscribe?.()
    }
  }, [bridge])

  const save = async () => {
    if (!draft || saving) return
    const validation = validatedDraft(draft)
    if (validation.error) {
      setError(t(`desktop.knowledgeBases.modelSettings.${validation.error}`))
      return
    }
    setSaving(true)
    setError(null)
    try {
      const result = await bridge.saveModelSettings(
        validation.settings,
        nextDesktopRequestId("model-settings"),
      )
      setSettings(result)
      setDraft(draftFrom(result))
      toast.success(t("desktop.knowledgeBases.modelSettings.saved"))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSaving(false)
    }
  }

  const testConnection = async () => {
    if (!draft || testing) return
    const validation = validatedDraft(draft)
    if (validation.error) {
      setError(t(`desktop.knowledgeBases.modelSettings.${validation.error}`))
      return
    }
    setTesting(true)
    setError(null)
    setTestResult(null)
    setTestStatus("queued")
    const requestId = nextDesktopRequestId("model-connection-test")
    testRequestIdRef.current = requestId
    try {
      const result = await bridge.testModelConnection(
        validation.settings,
        requestId,
      )
      if (!result.ok) throw new Error(t("desktop.knowledgeBases.modelSettings.testRejected"))
      const message = t("desktop.knowledgeBases.modelSettings.testSucceeded", {
        latency: result.latencyMs,
        attempts: result.attemptCount,
      })
      setTestResult(message)
      toast.success(message)
    } catch (reason) {
      if (reason instanceof DesktopBridgeError && reason.code === "request_cancelled") {
        setTestStatus("cancelled")
        toast.info(t("desktop.knowledgeBases.modelSettings.lifecycle.cancelled"))
      } else {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      if (testRequestIdRef.current === requestId) testRequestIdRef.current = null
      setTesting(false)
    }
  }

  const stopConnectionTest = async () => {
    const requestId = testRequestIdRef.current
    if (!requestId) return
    try {
      const result = await bridge.cancel(requestId)
      if (!result.cancelled) {
        setError(t("desktop.knowledgeBases.modelSettings.stopRejected"))
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const exportDiagnostics = async () => {
    if (exporting) return
    setError(null)
    try {
      const { save: chooseDestination } = await import("@tauri-apps/plugin-dialog")
      const destination = await chooseDestination({
        defaultPath: "openkb-desktop-diagnostics.zip",
        filters: [{ name: "ZIP", extensions: ["zip"] }],
      })
      if (!destination) return
      setExporting(true)
      const bundle = await bridge.exportDiagnosticBundle(
        destination,
        nextDesktopRequestId("diagnostic-bundle"),
      )
      toast.success(t("desktop.knowledgeBases.modelSettings.diagnosticsExported", { path: bundle.path }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setExporting(false)
    }
  }

  const revealKnowledgeBaseDirectory = async () => {
    setError(null)
    try {
      await bridge.revealKnowledgeBaseDirectory(kbDir)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const revealApplicationLogDirectory = async () => {
    setError(null)
    try {
      await bridge.revealApplicationLogDirectory()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  if (loading || !draft || !settings) {
    return (
      <div className="mt-8 flex items-center gap-2 rounded-apple-lg border border-border/70 bg-muted/20 p-5 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        {t("desktop.knowledgeBases.modelSettings.loading")}
      </div>
    )
  }

  return (
    <div className="mt-8 space-y-5" data-testid="desktop-model-settings">
      <section className="rounded-apple-lg border border-border/70 bg-background p-5 shadow-sm">
        <div className="flex items-start gap-3">
          <SlidersHorizontal className="mt-0.5 size-5 text-primary" />
          <div>
            <h2 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.title")}</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              {t("desktop.knowledgeBases.modelSettings.description")}
            </p>
          </div>
        </div>

        {error ? <p className="mt-4 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive" role="alert">{error}</p> : null}
        {testResult ? <p className="mt-4 flex items-center gap-2 rounded-lg border border-emerald-600/25 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300"><CheckCircle2 className="size-4" />{testResult}</p> : null}
        {testing && testStatus ? <p className="mt-4 flex items-center gap-2 rounded-lg border border-border/70 bg-muted/20 px-3 py-2 text-sm text-muted-foreground" role="status"><Loader2 className="size-4 animate-spin" />{t(`desktop.knowledgeBases.modelSettings.lifecycle.${testStatus}`)}</p> : null}

        <div className="mt-5 grid gap-4 md:grid-cols-2">
          <label className="block text-sm font-medium">
            {t("desktop.knowledgeBases.modelSettings.provider")}
            <select
              className="mt-1.5 flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
              value={draft.provider}
              disabled={saving}
              onChange={(event) => setDraft((current) => {
                if (!current) return current
                const provider = event.target.value
                return {
                  ...current,
                  provider,
                  apiBaseUrl: provider === "deepseek" ? "https://api.deepseek.com" : current.apiBaseUrl,
                }
              })}
            >
              <option value="deepseek">{t("desktop.knowledgeBases.modelSettings.providerDeepSeek")}</option>
              <option value="custom">{t("desktop.knowledgeBases.modelSettings.providerCustom")}</option>
            </select>
            <span className="mt-1 block text-xs font-normal leading-5 text-muted-foreground">
              {t("desktop.knowledgeBases.modelSettings.providerHelp")}
            </span>
          </label>
          <label className="block text-sm font-medium">
            {t("desktop.knowledgeBases.modelSettings.apiBaseUrl")}
            <Input className="mt-1.5 font-mono2" value={draft.apiBaseUrl} disabled={saving} onChange={(event) => setDraft((current) => current ? { ...current, apiBaseUrl: event.target.value } : current)} />
          </label>
          <label className="block text-sm font-medium md:col-span-2">
            {t("desktop.knowledgeBases.modelSettings.apiKey")}
            <span className="mt-1.5 flex gap-2">
              <Input type={showApiKey ? "text" : "password"} className="font-mono2" value={draft.apiKey} disabled={saving} onChange={(event) => setDraft((current) => current ? { ...current, apiKey: event.target.value } : current)} />
              <Button type="button" variant="outline" size="icon" disabled={saving} onClick={() => setShowApiKey((visible) => !visible)} aria-label={showApiKey ? t("desktop.knowledgeBases.modelSettings.hideApiKey") : t("desktop.knowledgeBases.modelSettings.showApiKey")}>
                {showApiKey ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </Button>
            </span>
          </label>
          <label className="block text-sm font-medium">
            {t("desktop.knowledgeBases.modelSettings.concurrency")}
            <Input className="mt-1.5" type="number" min="1" max="4" value={draft.maxConcurrentModelCalls} disabled={saving} onChange={(event) => setDraft((current) => current ? { ...current, maxConcurrentModelCalls: event.target.value } : current)} />
          </label>
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/70 bg-muted/20 px-4 py-3 text-sm">
          <span className="flex items-center gap-2 text-muted-foreground"><KeyRound className="size-4" />{settings.apiKeyConfigured ? t("desktop.knowledgeBases.modelSettings.apiKeyConfigured") : t("desktop.knowledgeBases.modelSettings.apiKeyRequired")}</span>
          <span className="text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.noResponseDeadline")}</span>
        </div>

        <div className="mt-6 border-t border-border/70 pt-5">
          <h3 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.rolesTitle")}</h3>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.rolesDescription")}</p>
          <div className="mt-4 grid gap-4 md:grid-cols-3">
            {roleFields.map((field) => (
              <label key={field.role} className="block text-sm font-medium">
                {t(`desktop.knowledgeBases.modelSettings.roles.${field.role}.model`)}
                <Input
                  className="mt-1.5"
                  value={draft[field.model]}
                  disabled={saving}
                  placeholder={field.role === "default" ? undefined : t("desktop.knowledgeBases.modelSettings.inheritDefaultModel")}
                  onChange={(event) => setDraft((current) => current ? { ...current, [field.model]: event.target.value } : current)}
                />
              </label>
            ))}
          </div>
          <div className="mt-4 grid gap-4 md:grid-cols-3">
            {roleFields.map((field) => (
              <label key={field.role} className="block text-sm font-medium">
                {t(`desktop.knowledgeBases.modelSettings.roles.${field.role}.context`)}
                <Input
                  className="mt-1.5"
                  type="number"
                  min="4096"
                  step="1024"
                  value={draft[field.context]}
                  disabled={saving}
                  placeholder={t("desktop.knowledgeBases.modelSettings.autoCapability")}
                  onChange={(event) => setDraft((current) => current ? { ...current, [field.context]: event.target.value } : current)}
                />
              </label>
            ))}
          </div>
          <div className="mt-4 grid gap-4 md:grid-cols-3">
            {roleFields.map((field) => (
              <label key={field.role} className="block text-sm font-medium">
                {t(`desktop.knowledgeBases.modelSettings.roles.${field.role}.reasoning`)}
                <select
                  className="mt-1.5 flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                  value={draft[field.reasoning]}
                  disabled={saving}
                  onChange={(event) => setDraft((current) => current ? {
                    ...current,
                    [field.reasoning]: event.target.value as "" | DesktopReasoningEffort,
                  } : current)}
                >
                  {reasoningOptions.map((option) => (
                    <option key={option || "provider"} value={option}>
                      {t(`desktop.knowledgeBases.modelSettings.reasoning.${option || "provider"}`)}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
        </div>

        <div className="mt-6 border-t border-border/70 pt-5">
          <h3 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.pricingTitle")}</h3>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.pricingDescription")}</p>
          <div className="mt-4 space-y-3">
            {roleFields.map((field) => (
              <div key={field.role} className="grid gap-3 rounded-lg border border-border/70 p-3 sm:grid-cols-[8rem_1fr_1fr]">
                <span className="self-center text-sm font-medium">{t(`desktop.knowledgeBases.modelSettings.roles.${field.role}.name`)}</span>
                <label className="text-xs text-muted-foreground">
                  {t("desktop.knowledgeBases.modelSettings.inputPrice")}
                  <Input type="number" min="0" step="0.000001" className="mt-1" value={draft[field.inputPrice]} disabled={saving} onChange={(event) => setDraft((current) => current ? { ...current, [field.inputPrice]: event.target.value } : current)} />
                </label>
                <label className="text-xs text-muted-foreground">
                  {t("desktop.knowledgeBases.modelSettings.outputPrice")}
                  <Input type="number" min="0" step="0.000001" className="mt-1" value={draft[field.outputPrice]} disabled={saving} onChange={(event) => setDraft((current) => current ? { ...current, [field.outputPrice]: event.target.value } : current)} />
                </label>
              </div>
            ))}
          </div>
        </div>

        <div className="mt-6 border-t border-border/70 pt-5">
          <h3 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.usageTitle")}</h3>
          <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-4">
            <div className="rounded-lg bg-muted/30 p-3"><dt className="text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.usageCalls")}</dt><dd className="mt-1 text-lg font-semibold">{settings.usageAggregate.callCount}</dd></div>
            <div className="rounded-lg bg-muted/30 p-3"><dt className="text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.usageAttempts")}</dt><dd className="mt-1 text-lg font-semibold">{settings.usageAggregate.attemptCount}</dd></div>
            <div className="rounded-lg bg-muted/30 p-3"><dt className="text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.usageTokens")}</dt><dd className="mt-1 text-lg font-semibold">{settings.usageAggregate.totalTokens.toLocaleString()}</dd><span className="text-xs text-muted-foreground">{settings.usageAggregate.tokenUsageSource ? t(`desktop.tasks.tokenSources.${settings.usageAggregate.tokenUsageSource}`) : t("desktop.tasks.tokenSources.unavailable")}</span></div>
            <div className="rounded-lg bg-muted/30 p-3"><dt className="text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.usageCost")}</dt><dd className="mt-1 text-lg font-semibold">{settings.usageAggregate.totalCost === null ? "—" : settings.usageAggregate.totalCost.toFixed(4)}</dd></div>
          </dl>
        </div>
        <p className="mt-3 text-xs leading-5 text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.testPolicy")}</p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="outline" disabled={saving} onClick={() => void (testing ? stopConnectionTest() : testConnection())}>{testing ? <Square className="size-4" /> : <CheckCircle2 className="size-4" />}{testing ? t("desktop.knowledgeBases.modelSettings.stopTesting") : t("desktop.knowledgeBases.modelSettings.testConnection")}</Button>
          <Button disabled={saving || testing} onClick={() => void save()}>{saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}{saving ? t("desktop.knowledgeBases.modelSettings.saving") : t("desktop.knowledgeBases.modelSettings.save")}</Button>
        </div>
      </section>

      <section className="rounded-apple-lg border border-border/70 bg-muted/20 p-5 shadow-sm">
        <h2 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.appearanceTitle")}</h2>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.appearanceDescription")}</p>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <label className="block text-sm font-medium">
            {t("desktop.knowledgeBases.modelSettings.theme")}
            <select className="mt-1.5 flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" value={theme} onChange={(event) => setTheme(event.target.value as "light" | "dark" | "system")}>
              <option value="system">{t("theme.system")}</option>
              <option value="light">{t("theme.light")}</option>
              <option value="dark">{t("theme.dark")}</option>
            </select>
          </label>
          <label className="block text-sm font-medium">
            {t("desktop.knowledgeBases.modelSettings.language")}
            <select className="mt-1.5 flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" value={language} onChange={(event) => setLanguage(event.target.value as "zh" | "en")}>
              <option value="zh">{t("language.zh")}</option>
              <option value="en">{t("language.en")}</option>
            </select>
          </label>
          <label className="block text-sm font-medium md:col-span-2">
            <span className="flex items-center justify-between"><span>{t("desktop.knowledgeBases.modelSettings.zoom")}</span><output>{zoom}%</output></span>
            <input className="mt-2 w-full accent-primary" type="range" min="80" max="200" step="10" value={zoom} onChange={(event) => setZoom(Number(event.target.value))} />
            <span className="mt-1 block text-xs font-normal text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.zoomHelp")}</span>
          </label>
        </div>
      </section>

      <section className="rounded-apple-lg border border-border/70 bg-muted/20 p-5 shadow-sm">
        <h2 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.storageTitle")}</h2>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.storageDescription")}</p>
        <div className="mt-4 flex flex-wrap gap-3">
          <Button variant="outline" onClick={() => void revealKnowledgeBaseDirectory()}>
            <FolderOpen className="size-4" />
            {t("desktop.knowledgeBases.modelSettings.openKnowledgeBaseDirectory")}
          </Button>
          <Button variant="outline" onClick={() => void revealApplicationLogDirectory()}>
            <FolderOpen className="size-4" />
            {t("desktop.knowledgeBases.modelSettings.openLogDirectory")}
          </Button>
        </div>
        <div className="mt-6 border-t border-border/70 pt-5">
          <h3 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.diagnosticsTitle")}</h3>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.diagnosticsDescription")}</p>
          <Button className="mt-4" variant="outline" disabled={exporting} onClick={() => setDiagnosticReviewOpen(true)}>
            {exporting ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
            {exporting ? t("desktop.knowledgeBases.modelSettings.exporting") : t("desktop.knowledgeBases.modelSettings.exportDiagnostics")}
          </Button>
        </div>
      </section>

      <Dialog open={diagnosticReviewOpen} onOpenChange={setDiagnosticReviewOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("desktop.knowledgeBases.modelSettings.diagnosticsReviewTitle")}</DialogTitle>
            <DialogDescription>
              {t("desktop.knowledgeBases.modelSettings.diagnosticsReviewDescription")}
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-lg border border-border/70 bg-muted/20 p-3 text-sm">
            <p className="font-medium">{t("desktop.knowledgeBases.modelSettings.diagnosticsIncludes")}</p>
            <ul className="mt-2 list-disc space-y-1 pl-5 font-mono2 text-xs text-muted-foreground">
              {diagnosticFiles.map((file) => <li key={file}>{file}</li>)}
            </ul>
          </div>
          <p className="text-sm leading-6 text-muted-foreground">
            {t("desktop.knowledgeBases.modelSettings.diagnosticsExcludes")}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDiagnosticReviewOpen(false)}>
              {t("desktop.knowledgeBases.modelSettings.cancel")}
            </Button>
            <Button
              onClick={() => {
                setDiagnosticReviewOpen(false)
                void exportDiagnostics()
              }}
            >
              <Download className="size-4" />
              {t("desktop.knowledgeBases.modelSettings.confirmExport")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
