import { FolderOpen, Plus } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/button"
import { DesktopConversationPanel } from "./DesktopConversationPanel"
import {
  DesktopDocumentImportPanel,
  type DesktopImportBatchSummary,
} from "./DesktopDocumentImportPanel"
import { DesktopKnowledgeWorkspace } from "./DesktopKnowledgeWorkspace"
import { DesktopModelSettingsPanel } from "./DesktopModelSettingsPanel"
import { DesktopOverviewPanel } from "./DesktopOverviewPanel"
import type {
  DesktopImportSourceInspection,
  DesktopImportSourcePicker,
  DesktopImportTask,
  DesktopKnowledgeBase,
} from "./contracts"
import type { WorkspaceSection } from "./DesktopWorkbenchShell"
import type { KnowledgeReanalysisController } from "./useKnowledgeReanalysis"

type ImportTaskAction = "pause" | "resume" | "cancel"

export function EmptyKnowledgeBase({ onCreate, onOpen }: { onCreate: () => void; onOpen: () => void }) {
  const { t } = useTranslation("common")
  return (
    <section className="mx-auto flex min-h-80 max-w-2xl flex-col justify-center rounded-apple-lg border border-border/70 bg-muted/20 p-8 shadow-sm">
      <span className="grid size-12 place-items-center rounded-2xl bg-primary/10">
        <img src="/openkb-mark.svg" alt="" className="size-9" />
      </span>
      <h1 className="mt-5 text-2xl font-semibold tracking-tight">{t("desktop.knowledgeBases.emptyTitle")}</h1>
      <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">{t("desktop.knowledgeBases.emptyDescription")}</p>
      <div className="mt-6 flex flex-wrap gap-3">
        <Button onClick={onCreate}><Plus className="size-4" />{t("desktop.knowledgeBases.create")}</Button>
        <Button variant="outline" onClick={onOpen}><FolderOpen className="size-4" />{t("desktop.knowledgeBases.open")}</Button>
      </div>
    </section>
  )
}

export function ActiveKnowledgeBaseView({
  knowledgeBase,
  section,
  importError,
  importing,
  importPath,
  importSources,
  importInspection,
  inspectingImportSources,
  importDropActive,
  importBatchSummary,
  importTasks,
  controllingJobId,
  knowledgeReanalysis,
  requestedConversationId,
  requestedConversationMessageId,
  requestedDocumentId,
  requestedKnowledgePageId,
  knowledgeInitialTab,
  navigationRequestSequence,
  onImportPathChange,
  onAddImportPath,
  onChooseImportSources,
  onRemoveImportSource,
  onSubmitImport,
  onControlImportJob,
  onOpenRawDocument,
  onNavigate,
  onOpenReview,
  onOpenFailedDocuments,
  onReviewChanged,
}: {
  knowledgeBase: DesktopKnowledgeBase
  section: WorkspaceSection
  importError: string | null
  importing: boolean
  importPath: string
  importSources: string[]
  importInspection: DesktopImportSourceInspection | null
  inspectingImportSources: boolean
  importDropActive: boolean
  importBatchSummary: DesktopImportBatchSummary | null
  importTasks: DesktopImportTask[]
  controllingJobId: string | null
  knowledgeReanalysis: KnowledgeReanalysisController
  requestedConversationId: string | null
  requestedConversationMessageId: string | null
  requestedDocumentId: string | null
  requestedKnowledgePageId: string | null
  knowledgeInitialTab: "pages" | "review"
  navigationRequestSequence: number
  onImportPathChange: (value: string) => void
  onAddImportPath: () => void
  onChooseImportSources: (picker: DesktopImportSourcePicker) => void
  onRemoveImportSource: (path: string) => void
  onSubmitImport: () => void
  onControlImportJob: (jobId: string, action: ImportTaskAction) => void
  onOpenRawDocument: (documentId: string, locator?: Record<string, unknown>) => void
  onNavigate: (section: WorkspaceSection) => void
  onOpenReview: () => void
  onOpenFailedDocuments: () => void
  onReviewChanged: () => void
}) {
  return (
    <section className="min-w-0">
      {section === "overview" ? <DesktopOverviewPanel tasks={importTasks} onImport={() => onNavigate("documents")} onStartConversation={() => onNavigate("conversations")} onOpenReview={onOpenReview} onOpenFailures={onOpenFailedDocuments} /> : null}
      {section === "documents" ? (
        <div className="mx-auto max-w-6xl">
          <DesktopDocumentImportPanel
            error={importError}
            importing={importing}
            manualPath={importPath}
            sources={importSources}
            inspection={importInspection}
            inspecting={inspectingImportSources}
            dropActive={importDropActive}
            summary={importBatchSummary}
            tasks={importTasks}
            controllingJobId={controllingJobId}
            knowledgeReanalysis={knowledgeReanalysis}
            requestedDocumentId={requestedDocumentId}
            requestKey={navigationRequestSequence}
            onManualPathChange={onImportPathChange}
            onAddManualPath={onAddImportPath}
            onChooseSources={onChooseImportSources}
            onRemoveSource={onRemoveImportSource}
            onSubmit={onSubmitImport}
            onControl={onControlImportJob}
            onOpenOriginal={onOpenRawDocument}
            onOpenFailedDocuments={onOpenFailedDocuments}
          />
        </div>
      ) : null}
      <div className={section === "conversations" ? "block" : "hidden"}>
        <DesktopConversationPanel key={knowledgeBase.kbDir} requestKey={navigationRequestSequence} requestedConversationId={requestedConversationId} requestedMessageId={requestedConversationMessageId} onOpenOriginal={onOpenRawDocument} onOpenModelSettings={() => onNavigate("settings")} />
      </div>
      {section === "knowledge" ? (
        <DesktopKnowledgeWorkspace
          requestKey={navigationRequestSequence}
          initialTab={knowledgeInitialTab}
          requestedPageId={requestedKnowledgePageId}
          onReviewChanged={onReviewChanged}
        />
      ) : null}
      {section === "settings" ? <div className="mx-auto max-w-5xl"><DesktopModelSettingsPanel key={knowledgeBase.kbDir} kbDir={knowledgeBase.kbDir} /></div> : null}
    </section>
  )
}
