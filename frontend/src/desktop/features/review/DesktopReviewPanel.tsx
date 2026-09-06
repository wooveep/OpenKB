import { DesktopDocumentVersionCandidatePanel } from "@/desktop/features/review/DesktopDocumentVersionCandidatePanel"
import { DesktopKnowledgeReconciliationPanel } from "@/desktop/features/review/DesktopKnowledgeReconciliationPanel"
import { DesktopSemanticReviewPanel } from "@/desktop/features/review/DesktopSemanticReviewPanel"

/** Keeps independent document identity review separate from knowledge conflicts. */
export function DesktopReviewPanel() {
  return (
    <>
      <DesktopSemanticReviewPanel />
      <DesktopKnowledgeReconciliationPanel />
      <DesktopDocumentVersionCandidatePanel />
    </>
  )
}
