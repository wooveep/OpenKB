import { FolderOpen } from "lucide-react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { useLanguage } from "@/lib/language-context"
import { useTheme } from "@/lib/theme-context"
import { useZoom } from "@/lib/zoom"
import { useDesktopBridge } from "@/desktop/bridge/context"

/** Settings that remain usable while the Python Engine is unavailable. */
export default function DesktopLocalSettingsPanel() {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const { language, setLanguage } = useLanguage()
  const { theme, setTheme } = useTheme()
  const { zoom, setZoom } = useZoom()

  const openLogs = async () => {
    try {
      await bridge.revealApplicationLogDirectory()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5" data-testid="desktop-local-settings">
      <section className="rounded-apple-lg border border-border/70 bg-background p-5 shadow-sm">
        <h1 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.appearanceTitle")}</h1>
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
        <h2 className="font-semibold">{t("desktop.knowledgeBases.modelSettings.diagnosticsTitle")}</h2>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">{t("desktop.engine.logsAvailableBeforeEngine")}</p>
        <Button className="mt-4" variant="outline" onClick={() => void openLogs()}>
          <FolderOpen className="size-4" />{t("desktop.knowledgeBases.modelSettings.openLogDirectory")}
        </Button>
      </section>
    </div>
  )
}
