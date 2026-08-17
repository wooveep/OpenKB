import { Download, Eye, EyeOff, FolderOpen, KeyRound, Loader2, Save, SlidersHorizontal } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
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
import { useDesktopBridge } from "./bridge-context"
import { nextDesktopRequestId } from "./request-id"
import type { DesktopModelSettings } from "./contracts"

type ModelSettingsDraft = {
  model: string
  apiBaseUrl: string
  apiKey: string
  maxConcurrentModelCalls: string
  initialTimeoutSeconds: string
}

const diagnosticFiles = [
  "manifest.json",
  "model-settings.json",
  "import-jobs.json",
  "model-calls.json",
  "graph-diagnostics.json",
  "integrity.json",
]

function draftFrom(settings: DesktopModelSettings): ModelSettingsDraft {
  return {
    model: settings.model,
    apiBaseUrl: settings.apiBaseUrl,
    apiKey: settings.apiKey,
    maxConcurrentModelCalls: String(settings.maxConcurrentModelCalls),
    initialTimeoutSeconds: String(settings.initialTimeoutSeconds),
  }
}

/** Model connection defaults and opt-in support diagnostics for the active knowledge base. */
export function DesktopModelSettingsPanel({ kbDir }: { kbDir: string }) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [settings, setSettings] = useState<DesktopModelSettings | null>(null)
  const [draft, setDraft] = useState<ModelSettingsDraft | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [diagnosticReviewOpen, setDiagnosticReviewOpen] = useState(false)
  const [showApiKey, setShowApiKey] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

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

  const save = async () => {
    if (!draft || saving) return
    const maxConcurrentModelCalls = Number(draft.maxConcurrentModelCalls)
    const initialTimeoutSeconds = Number(draft.initialTimeoutSeconds)
    if (!Number.isInteger(maxConcurrentModelCalls) || maxConcurrentModelCalls < 1 || maxConcurrentModelCalls > 8) {
      setError(t("desktop.knowledgeBases.modelSettings.invalidConcurrency"))
      return
    }
    if (!Number.isFinite(initialTimeoutSeconds) || initialTimeoutSeconds <= 0 || initialTimeoutSeconds > 60) {
      setError(t("desktop.knowledgeBases.modelSettings.invalidTimeout"))
      return
    }
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const result = await bridge.saveModelSettings(
        draft.model,
        draft.apiBaseUrl,
        draft.apiKey,
        maxConcurrentModelCalls,
        initialTimeoutSeconds,
        nextDesktopRequestId("model-settings"),
      )
      setSettings(result)
      setDraft(draftFrom(result))
      setNotice(t("desktop.knowledgeBases.modelSettings.saved"))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSaving(false)
    }
  }

  const exportDiagnostics = async () => {
    if (exporting) return
    setError(null)
    setNotice(null)
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
      setNotice(t("desktop.knowledgeBases.modelSettings.diagnosticsExported", { path: bundle.path }))
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
        {notice ? <p className="mt-4 rounded-lg border border-emerald-600/25 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">{notice}</p> : null}

        <div className="mt-5 grid gap-4 md:grid-cols-2">
          <label className="block text-sm font-medium">
            {t("desktop.knowledgeBases.modelSettings.model")}
            <Input className="mt-1.5" value={draft.model} disabled={saving} onChange={(event) => setDraft((current) => current ? { ...current, model: event.target.value } : current)} />
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
            <Input className="mt-1.5" type="number" min="1" max="8" value={draft.maxConcurrentModelCalls} disabled={saving} onChange={(event) => setDraft((current) => current ? { ...current, maxConcurrentModelCalls: event.target.value } : current)} />
          </label>
          <label className="block text-sm font-medium">
            {t("desktop.knowledgeBases.modelSettings.timeout")}
            <Input className="mt-1.5" type="number" min="1" max="60" value={draft.initialTimeoutSeconds} disabled={saving} onChange={(event) => setDraft((current) => current ? { ...current, initialTimeoutSeconds: event.target.value } : current)} />
          </label>
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/70 bg-muted/20 px-4 py-3 text-sm">
          <span className="flex items-center gap-2 text-muted-foreground"><KeyRound className="size-4" />{settings.apiKeyConfigured ? t("desktop.knowledgeBases.modelSettings.apiKeyConfigured") : t("desktop.knowledgeBases.modelSettings.apiKeyRequired")}</span>
          <span className="text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.deadline", { seconds: settings.modelCallDeadlineSeconds })}</span>
        </div>
        <div className="mt-5 flex justify-end"><Button disabled={saving} onClick={() => void save()}>{saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}{saving ? t("desktop.knowledgeBases.modelSettings.saving") : t("desktop.knowledgeBases.modelSettings.save")}</Button></div>
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
      </section>

      <section className="rounded-apple-lg border border-border/70 bg-muted/20 p-5 shadow-sm">
        <h2 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.diagnosticsTitle")}</h2>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">{t("desktop.knowledgeBases.modelSettings.diagnosticsDescription")}</p>
        <Button className="mt-4" variant="outline" disabled={exporting} onClick={() => setDiagnosticReviewOpen(true)}>
          {exporting ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
          {exporting ? t("desktop.knowledgeBases.modelSettings.exporting") : t("desktop.knowledgeBases.modelSettings.exportDiagnostics")}
        </Button>
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
