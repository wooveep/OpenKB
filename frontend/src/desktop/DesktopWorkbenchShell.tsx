import {
  Activity,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  CirclePlus,
  Command,
  FileText,
  FolderOpen,
  LayoutDashboard,
  MessageSquare,
  Settings,
} from "lucide-react"
import { useState, type ComponentType, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export type WorkspaceSection =
  | "overview"
  | "documents"
  | "conversations"
  | "knowledge"
  | "settings"

const NAVIGATION_COLLAPSED_KEY = "openkb.desktop.navigation-collapsed"

const navigation: Array<{
  id: WorkspaceSection
  icon: ComponentType<{ className?: string }>
  labelKey: string
}> = [
  { id: "overview", icon: LayoutDashboard, labelKey: "overview" },
  { id: "documents", icon: FileText, labelKey: "documents" },
  { id: "conversations", icon: MessageSquare, labelKey: "conversations" },
  { id: "knowledge", icon: BookOpen, labelKey: "knowledge" },
  { id: "settings", icon: Settings, labelKey: "settings" },
]

/** Content-first frame shared by every Desktop Knowledge Base workspace. */
export function DesktopWorkbenchShell({
  activeSection,
  knowledgeBaseName,
  engineReady,
  activeTaskCount,
  reviewCount,
  onOpenReview,
  onSectionChange,
  onOpenKnowledgeBase,
  onCreateKnowledgeBase,
  onOpenSearch,
  onOpenTasks,
  children,
}: {
  activeSection: WorkspaceSection
  knowledgeBaseName: string | null
  engineReady: boolean
  activeTaskCount: number
  reviewCount: number
  onOpenReview: () => void
  onSectionChange: (section: WorkspaceSection) => void
  onOpenKnowledgeBase: () => void
  onCreateKnowledgeBase: () => void
  onOpenSearch: () => void
  onOpenTasks: () => void
  children: ReactNode
}) {
  const { t } = useTranslation("common")
  const [collapsed, setCollapsed] = useState(() => (
    typeof window !== "undefined" && window.localStorage.getItem(NAVIGATION_COLLAPSED_KEY) === "true"
  ))

  const toggleCollapsed = () => {
    setCollapsed((current) => {
      const next = !current
      window.localStorage.setItem(NAVIGATION_COLLAPSED_KEY, String(next))
      return next
    })
  }

  return (
    <div className="min-h-screen bg-background text-foreground" data-testid="desktop-workbench">
      <header className="flex min-h-14 items-center justify-between gap-3 border-b border-border/70 bg-background/90 px-3 backdrop-blur md:px-4">
        <div className="flex min-w-0 items-center gap-2">
          <div className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground shadow-sm">
            <BookOpen className="size-4" />
          </div>
          <button
            type="button"
            onClick={onOpenKnowledgeBase}
            className="flex min-w-0 max-w-[min(42vw,25rem)] items-center gap-1.5 rounded-md px-2 py-1 text-sm font-semibold outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
            aria-haspopup="dialog"
          >
            <span className="truncate">
              {knowledgeBaseName ?? t("desktop.knowledgeBases.chooseKnowledgeBase")}
            </span>
            <FolderOpen className="size-3.5 shrink-0 text-muted-foreground" />
          </button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-8"
            onClick={onCreateKnowledgeBase}
            aria-label={t("desktop.knowledgeBases.create")}
          >
            <CirclePlus className="size-4" />
          </Button>
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <Button type="button" variant="outline" size="sm" onClick={onOpenSearch}>
            <Command className="size-3.5" />
            <span className="hidden sm:inline">{t("desktop.knowledgeBases.globalSearch")}</span>
            <kbd className="hidden rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground md:inline">
              Ctrl K
            </kbd>
          </Button>
          <Button type="button" variant="outline" size="sm" onClick={onOpenTasks}>
            <Activity className="size-3.5" />
            <span className="hidden sm:inline">{t("desktop.knowledgeBases.tasks")}</span>
            {activeTaskCount > 0 ? (
              <span className="rounded-full bg-primary px-1.5 py-0.5 text-[10px] text-primary-foreground">
                {activeTaskCount}
              </span>
            ) : null}
          </Button>
          <span
            className="flex items-center gap-1.5 rounded-full border border-border/70 px-2.5 py-1.5 text-xs text-muted-foreground"
            role="status"
          >
            <span className={cn("size-2 rounded-full", engineReady ? "bg-emerald-500" : "bg-amber-500")} />
            <span className="hidden md:inline">
              {t(engineReady ? "desktop.engine.ready.short" : "desktop.engine.starting.short")}
            </span>
          </span>
        </div>
      </header>

      <div
        className={cn(
          "grid min-h-[calc(100vh-3.5rem)] transition-[grid-template-columns] duration-150 motion-reduce:transition-none",
          collapsed ? "grid-cols-[3.75rem_minmax(0,1fr)]" : "grid-cols-[12rem_minmax(0,1fr)]",
        )}
      >
        <aside className="border-r border-border/70 bg-muted/15 p-2" aria-label={t("desktop.knowledgeBases.navigation")}>
          <nav className="space-y-1">
            {navigation.map(({ id, icon: Icon, labelKey }) => (
              <div key={id} className="relative flex items-center">
                <button
                type="button"
                onClick={() => onSectionChange(id)}
                aria-current={activeSection === id ? "page" : undefined}
                title={collapsed ? t(`desktop.knowledgeBases.navigationItems.${labelKey}`) : undefined}
                className={cn(
                  "flex h-9 w-full items-center rounded-md text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
                  collapsed ? "justify-center px-2" : "gap-2 px-3 text-left",
                  activeSection === id
                    ? "bg-primary text-primary-foreground shadow-sm"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                )}
              >
                <Icon className="size-4 shrink-0" />
                {collapsed ? null : t(`desktop.knowledgeBases.navigationItems.${labelKey}`)}
                </button>
                {id === "knowledge" && reviewCount > 0 ? (
                  <button type="button" onClick={onOpenReview} aria-label={t("desktop.knowledgeBases.navigationItems.review")} className={cn("absolute rounded-full px-1.5 py-0.5 text-[10px]", activeSection === id ? "bg-primary-foreground/20" : "bg-primary/10 text-primary", collapsed ? "right-0 top-0" : "right-2")}>
                    {reviewCount}
                  </button>
                ) : null}
              </div>
            ))}
          </nav>
          <button
            type="button"
            onClick={toggleCollapsed}
            className={cn(
              "mt-4 flex h-8 w-full items-center rounded-md text-xs text-muted-foreground outline-none hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring",
              collapsed ? "justify-center" : "justify-between px-3",
            )}
            aria-label={t(collapsed ? "desktop.knowledgeBases.expandNavigation" : "desktop.knowledgeBases.collapseNavigation")}
          >
            {collapsed ? <ChevronRight className="size-4" /> : (
              <>
                <span>{t("desktop.knowledgeBases.collapseNavigation")}</span>
                <ChevronLeft className="size-4" />
              </>
            )}
          </button>
        </aside>

        <main className="min-w-0 overflow-hidden">{children}</main>
      </div>
    </div>
  )
}
