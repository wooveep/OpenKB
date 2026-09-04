import type {
  DesktopDocumentLineageDecision,
  DesktopDocumentVersionCandidate,
  DesktopDocumentVersionCandidateDecision,
  DesktopDocumentVersionCandidates,
  DesktopDocumentVersionCatalog,
  DesktopDocumentVersionDiff,
  DesktopDocumentVersionDiffs,
  DesktopImportTask,
} from "./contracts"

/** In-memory implementation of the Document Version interface used by the preview Bridge. */
export class MemoryDocumentVersionStore {
  private candidates: DesktopDocumentVersionCandidate[] = []
  private catalog: DesktopDocumentVersionCatalog = emptyCatalog()
  private diffs: DesktopDocumentVersionDiff[] = []

  reset(): void {
    this.candidates = []
    this.catalog = emptyCatalog()
    this.diffs = []
  }

  pendingCandidates(): DesktopDocumentVersionCandidates {
    return { candidates: this.candidates.filter((candidate) => candidate.status === "pending") }
  }

  catalogSnapshot(): DesktopDocumentVersionCatalog {
    return this.catalog
  }

  confirmLineage(
    decision: DesktopDocumentLineageDecision,
    requestId: string,
    importTasks: DesktopImportTask[],
  ): DesktopDocumentVersionCatalog {
    const imported = new Map(
      importTasks
        .flatMap((task) => task.document ? [task.document] : [])
        .map((document) => [document.documentId, document]),
    )
    const lineageId = decision.lineageId ?? `lineage-${requestId}`
    const metadataRevision = 1 + Math.max(
      0,
      ...decision.expectedMetadataRevisions.map((item) => item.metadataRevision),
    )
    const lineage = {
      lineageId,
      displayName: decision.displayName,
      normalizedName: decision.displayName.trim().toLowerCase(),
      lineageState: "confirmed" as const,
      versionScheme: decision.versionScheme,
      currentDocumentId: decision.currentDocumentId,
      metadataRevision,
      aliases: decision.aliases,
      members: decision.members.map((member) => ({
        documentId: member.documentId,
        documentName: imported.get(member.documentId)?.name ?? member.documentId,
        availability: imported.get(member.documentId)?.availability ?? "available",
        versionLabel: member.versionLabel,
        normalizedVersionLabel: member.versionLabel.trim().toLowerCase(),
        versionKeyJson: null,
        branchLabel: member.branchLabel ?? "main",
        predecessorDocumentId: member.predecessorDocumentId,
        snapshotKind: member.snapshotKind ?? "full_snapshot" as const,
        metadataOrigin: member.metadataOrigin ?? "user",
        confirmedAt: new Date().toISOString(),
      })),
    }
    this.catalog = {
      ...this.catalog,
      revisionId: `memory-versions-${requestId}`,
      lineages: [
        ...this.catalog.lineages.filter((item) => (
          !decision.members.some((member) => (
            item.members.some((existing) => existing.documentId === member.documentId)
          ))
        )),
        lineage,
      ],
    }
    return this.catalog
  }

  diffsForLineage(lineageId: string): DesktopDocumentVersionDiffs {
    return { diffs: this.diffs.filter((diff) => diff.lineageId === lineageId) }
  }

  resolveCandidate(
    candidateId: string,
    decision: DesktopDocumentVersionCandidateDecision,
  ): DesktopDocumentVersionCandidate {
    const candidate = this.candidates.find((item) => item.candidateId === candidateId)
    if (!candidate) throw new Error("The selected document version candidate was not found.")
    if (candidate.status !== "pending") {
      throw new Error("The selected document version candidate is resolved.")
    }
    const status: DesktopDocumentVersionCandidate["status"] = decision === "link_to_candidate"
      ? "accepted"
      : "rejected"
    const resolved = { ...candidate, status }
    this.candidates = this.candidates.map((item) => (
      item.documentId === candidate.documentId
        ? item.candidateId === candidateId ? resolved : { ...item, status: "dismissed" }
        : item
    ))
    return resolved
  }
}

function emptyCatalog(): DesktopDocumentVersionCatalog {
  return {
    revisionId: "memory-versions-0",
    sourceRevision: 0,
    snapshotDigest: "memory-empty",
    lineages: [],
  }
}
