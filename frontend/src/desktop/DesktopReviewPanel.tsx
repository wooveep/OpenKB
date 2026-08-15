import { DesktopDocumentVersionCandidatePanel } from "./DesktopDocumentVersionCandidatePanel"
import { DesktopKnowledgeReconciliationPanel } from "./DesktopKnowledgeReconciliationPanel"

/** Keeps independent document identity review separate from knowledge conflicts. */
export function DesktopReviewPanel() {
  return (
    <>
      <DesktopKnowledgeReconciliationPanel />
      <DesktopDocumentVersionCandidatePanel />
    </>
  )
}
