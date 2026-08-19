import { Download, Loader2, PackageOpen } from "lucide-react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useDesktopBridge } from "./bridge-context"
import type { DesktopKnowledgeExportMode } from "./contracts"
import { DesktopDocumentVersionCandidatePanel } from "./DesktopDocumentVersionCandidatePanel"
import { DesktopKnowledgePagePanel } from "./DesktopKnowledgePagePanel"
import { DesktopKnowledgeReconciliationPanel } from "./DesktopKnowledgeReconciliationPanel"
import { DesktopMissingSourcePanel } from "./DesktopMissingSourcePanel"
import { nextDesktopRequestId } from "./request-id"

/** Knowledge pages, review queues, and explicit portable exports share one workspace. */
export function DesktopKnowledgeWorkspace({
  initialTab = "pages",
  requestedPageId,
  requestKey = 0,
  onReviewChanged,
}: {
  initialTab?: "pages" | "review"
  requestedPageId?: string | null
  requestKey?: number
  onReviewChanged?: () => void
}) {
  const { t } = useTranslation("common")
  const bridge = useDesktopBridge()
  const [exporting, setExporting] = useState(false)
  const [knowledgeReviewRevision, setKnowledgeReviewRevision] = useState(0)

  const handleMissingSourceResolved = () => {
    setKnowledgeReviewRevision((revision) => revision + 1)
    onReviewChanged?.()
  }

  const exportKnowledge = async (mode: DesktopKnowledgeExportMode) => {
    if (exporting) return
    const destination = await bridge.chooseKnowledgeBaseDirectory()
    if (!destination) return
    setExporting(true)
    try {
      const exported = await bridge.exportKnowledgeBundle(
        destination,
        mode,
        nextDesktopRequestId("knowledge-export"),
      )
      toast.success(t("desktop.knowledge.export.success", { path: exported.path }))
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : t("desktop.knowledge.export.failed"),
      )
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="mx-auto max-w-6xl py-2" data-testid="desktop-knowledge-workspace">
      <Tabs key={`${initialTab}:${requestKey}`} defaultValue={initialTab}>
        <div className="flex items-center justify-between gap-3">
          <TabsList>
            <TabsTrigger value="pages">{t("desktop.knowledge.tabs.pages")}</TabsTrigger>
            <TabsTrigger value="review">{t("desktop.knowledge.tabs.review")}</TabsTrigger>
          </TabsList>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" disabled={exporting}>
                {exporting ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Download className="size-4" />
                )}
                {exporting
                  ? t("desktop.knowledge.export.exporting")
                  : t("desktop.knowledge.export.action")}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-72">
              <DropdownMenuItem
                className="items-start py-2"
                onSelect={() => void exportKnowledge("knowledge_projection")}
              >
                <Download className="mt-0.5 size-4" />
                <span>
                  <span className="block font-medium">
                    {t("desktop.knowledge.export.projection")}
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    {t("desktop.knowledge.export.projectionDescription")}
                  </span>
                </span>
              </DropdownMenuItem>
              <DropdownMenuItem
                className="items-start py-2"
                onSelect={() => void exportKnowledge("self_contained")}
              >
                <PackageOpen className="mt-0.5 size-4" />
                <span>
                  <span className="block font-medium">
                    {t("desktop.knowledge.export.selfContained")}
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    {t("desktop.knowledge.export.selfContainedDescription")}
                  </span>
                </span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <TabsContent value="pages">
          <DesktopKnowledgePagePanel requestedPageId={requestedPageId} />
        </TabsContent>
        <TabsContent value="review">
          <Tabs defaultValue="missing_sources" className="mt-5">
            <TabsList>
              <TabsTrigger value="missing_sources">
                {t("desktop.knowledge.tabs.missingSources")}
              </TabsTrigger>
              <TabsTrigger value="conflicts">
                {t("desktop.knowledge.tabs.conflicts")}
              </TabsTrigger>
              <TabsTrigger value="versions">
                {t("desktop.knowledge.tabs.versions")}
              </TabsTrigger>
            </TabsList>
            <TabsContent value="missing_sources">
              <DesktopMissingSourcePanel onResolved={handleMissingSourceResolved} />
            </TabsContent>
            <TabsContent value="conflicts">
              <DesktopKnowledgeReconciliationPanel refreshKey={knowledgeReviewRevision} />
            </TabsContent>
            <TabsContent value="versions">
              <DesktopDocumentVersionCandidatePanel />
            </TabsContent>
          </Tabs>
        </TabsContent>
      </Tabs>
    </div>
  )
}
