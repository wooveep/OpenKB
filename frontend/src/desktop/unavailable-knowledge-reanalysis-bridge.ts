import type {
  DesktopKnowledgeReanalysisOverview,
  DesktopKnowledgeReanalysisRun,
} from "./knowledge-reanalysis-contracts"
import { UnavailableKnowledgePageBridge } from "./unavailable-knowledge-page-bridge"

/** Unavailable-shell implementations for the Reanalysis bridge surface. */
export abstract class UnavailableKnowledgeReanalysisBridge extends UnavailableKnowledgePageBridge {
  protected abstract unavailable<T>(): Promise<T>

  knowledgeReanalysis(): Promise<DesktopKnowledgeReanalysisOverview> {
    return this.unavailable()
  }

  startKnowledgeReanalysis(
    documentIds: string[],
    requestId: string,
  ): Promise<DesktopKnowledgeReanalysisRun> {
    void documentIds
    void requestId
    return this.unavailable()
  }

  retryKnowledgeReanalysis(
    jobId: string,
    requestId: string,
  ): Promise<DesktopKnowledgeReanalysisRun> {
    void jobId
    void requestId
    return this.unavailable()
  }
}
