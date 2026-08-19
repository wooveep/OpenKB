import type {
  DesktopKnowledgeReanalysisOverview,
  DesktopKnowledgeReanalysisRun,
} from "./knowledge-reanalysis-contracts"
import { TauriKnowledgeReviewBridge } from "./tauri-knowledge-review-bridge"

/** Typed Tauri calls for explicit Knowledge Reanalysis work. */
export abstract class TauriKnowledgeReanalysisBridge extends TauriKnowledgeReviewBridge {
  async knowledgeReanalysis(): Promise<DesktopKnowledgeReanalysisOverview> {
    return this.call("desktop_knowledge_reanalysis")
  }

  async startKnowledgeReanalysis(
    documentIds: string[],
    requestId: string,
  ): Promise<DesktopKnowledgeReanalysisRun> {
    return this.call("desktop_start_knowledge_reanalysis", { documentIds, requestId })
  }

  async retryKnowledgeReanalysis(
    jobId: string,
    requestId: string,
  ): Promise<DesktopKnowledgeReanalysisRun> {
    return this.call("desktop_retry_knowledge_reanalysis", { jobId, requestId })
  }
}
