import { useTranslation } from "react-i18next"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { DesktopDocumentVersionCandidatePanel } from "./DesktopDocumentVersionCandidatePanel"
import { DesktopKnowledgePagePanel } from "./DesktopKnowledgePagePanel"
import { DesktopKnowledgeReconciliationPanel } from "./DesktopKnowledgeReconciliationPanel"

/** Knowledge pages and both review queues share one stable workspace. */
export function DesktopKnowledgeWorkspace({ initialTab = "pages", requestedPageId, requestKey = 0 }: { initialTab?: "pages" | "review"; requestedPageId?: string | null; requestKey?: number }) {
  const { t } = useTranslation("common")
  return (
    <div className="mx-auto max-w-6xl py-2" data-testid="desktop-knowledge-workspace">
      <Tabs key={`${initialTab}:${requestKey}`} defaultValue={initialTab}>
        <TabsList>
          <TabsTrigger value="pages">{t("desktop.knowledge.tabs.pages")}</TabsTrigger>
          <TabsTrigger value="review">{t("desktop.knowledge.tabs.review")}</TabsTrigger>
        </TabsList>
        <TabsContent value="pages"><DesktopKnowledgePagePanel requestedPageId={requestedPageId} /></TabsContent>
        <TabsContent value="review">
          <Tabs defaultValue="conflicts" className="mt-5">
            <TabsList>
              <TabsTrigger value="conflicts">{t("desktop.knowledge.tabs.conflicts")}</TabsTrigger>
              <TabsTrigger value="versions">{t("desktop.knowledge.tabs.versions")}</TabsTrigger>
            </TabsList>
            <TabsContent value="conflicts"><DesktopKnowledgeReconciliationPanel /></TabsContent>
            <TabsContent value="versions"><DesktopDocumentVersionCandidatePanel /></TabsContent>
          </Tabs>
        </TabsContent>
      </Tabs>
    </div>
  )
}
