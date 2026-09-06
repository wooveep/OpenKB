/** User-confirmed Document Lineage, Version Catalog, and deterministic Diff contracts. */

export type DesktopDocumentVersionCandidateDecision = "link_to_candidate" | "keep_separate"

export interface DesktopDocumentVersionCandidate {
  candidateId: string
  documentId: string
  documentName: string
  candidateDocumentId: string
  candidateDocumentName: string
  lexicalScore: number
  characterScore: number
  reason: "lexical_character_similarity"
  status: "pending" | "accepted" | "rejected" | "dismissed"
}

export interface DesktopDocumentVersionCandidates {
  candidates: DesktopDocumentVersionCandidate[]
}

export type DesktopVersionMode = "latest" | "exact" | "compare" | "all" | "unscoped"
export type DesktopVersionScheme = "numeric_dotted" | "semver" | "calendar" | "opaque"
export type DesktopSnapshotKind = "full_snapshot" | "delta" | "unknown"

export interface DesktopVersionFilter {
  mode: DesktopVersionMode | null
  lineageIds: string[]
  versionLabels: string[]
  documentIds: string[]
}

export interface DesktopDocumentVersionMemberDecision {
  documentId: string
  versionLabel: string
  branchLabel?: string
  predecessorDocumentId: string | null
  snapshotKind?: DesktopSnapshotKind
  metadataOrigin?: string
}

export interface DesktopExpectedLineageRevision {
  lineageId: string
  metadataRevision: number
}

export interface DesktopDocumentLineageDecision {
  displayName: string
  versionScheme: DesktopVersionScheme
  members: DesktopDocumentVersionMemberDecision[]
  currentDocumentId: string
  aliases: string[]
  lineageId: string | null
  expectedMetadataRevisions: DesktopExpectedLineageRevision[]
}

export interface DesktopDocumentVersionCatalogMember {
  documentId: string
  documentName: string
  availability: string
  versionLabel: string | null
  normalizedVersionLabel: string | null
  versionKeyJson: string | null
  branchLabel: string | null
  predecessorDocumentId: string | null
  snapshotKind: DesktopSnapshotKind
  metadataOrigin: string | null
  confirmedAt: string | null
}

export interface DesktopDocumentLineage {
  lineageId: string
  displayName: string
  normalizedName: string
  lineageState: "singleton" | "needs_order_review" | "confirmed"
  versionScheme: DesktopVersionScheme
  currentDocumentId: string | null
  metadataRevision: number
  aliases: string[]
  members: DesktopDocumentVersionCatalogMember[]
}

export interface DesktopDocumentVersionCatalog {
  revisionId: string
  sourceRevision: number
  snapshotDigest: string
  lineages: DesktopDocumentLineage[]
}

export interface DesktopVersionDiffItem {
  oldBlockId: string | null
  newBlockId: string | null
  oldEvidenceId: string | null
  newEvidenceId: string | null
  contentChangeKind: "unchanged" | "modified" | "added" | "removed"
  locationChangeKind: "same" | "moved" | "unknown"
  similarityScore: number
  reasonJson: string
  oldLocator: Record<string, unknown> | null
  newLocator: Record<string, unknown> | null
}

export interface DesktopDocumentVersionDiff {
  diffId: string
  lineageId: string
  fromDocumentId: string
  toDocumentId: string
  algorithmVersion: string
  status: "ready" | "stale" | "failed"
  stats: Record<string, number>
  items: DesktopVersionDiffItem[]
}

export interface DesktopDocumentVersionDiffs {
  diffs: DesktopDocumentVersionDiff[]
}
