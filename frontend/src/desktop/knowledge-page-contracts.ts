/** Generated and user-authored Knowledge Workspace authority contracts. */

export type DesktopKnowledgePageKind = "concept" | "entity" | "procedure"
export type DesktopKnowledgePagePublicationState = "draft" | "unpublished_changes" | "published"
export type DesktopKnowledgeLifecycleState = "draft" | "stable" | "deprecated"
export type DesktopKnowledgeProvenanceState = "source_backed" | "structural" | "legacy_unmapped" | "unsourced" | "invalid"
export type DesktopKnowledgeVerificationState = "unverified" | "human_reviewed"
export type DesktopKnowledgeVerificationReason =
  | "publish_required"
  | "working_draft_not_verifiable"
  | "not_verified"
  | "revision_changed"
  | "publication_gate_blocked"
  | "legacy_unmapped_not_verifiable"
  | "deprecated_not_verifiable"
  | "lifecycle_changed"

export interface DesktopKnowledgeVerificationStatus {
  state: DesktopKnowledgeVerificationState
  canVerify: boolean
  reason: DesktopKnowledgeVerificationReason | null
  actor: string | null
  verifiedAt: string | null
  revisionId: string | null
}

export interface DesktopKnowledgePageSummary {
  pageId: string
  kind: DesktopKnowledgePageKind
  title: string
  publicationState: DesktopKnowledgePagePublicationState
  publishedRevisionNumber: number | null
  updatedAt: string
  lifecycleState: DesktopKnowledgeLifecycleState
  staleAfter: string | null
  isStale: boolean
}

export interface DesktopKnowledgeSourceCandidate {
  evidenceId: string
  documentId: string
  documentName: string
  section: string
  locator: Record<string, unknown>
  excerpt: string
}

export interface DesktopKnowledgeSourceMapEntry extends DesktopKnowledgeSourceCandidate {
  sourceId: string
  claimText: string
  availability: "available" | "unavailable"
}

export interface DesktopKnowledgePublicationDiagnostic {
  code: string
  message: string
  sourceId: string
}

export interface DesktopKnowledgePublishedRevision {
  revisionNumber: number
  title: string
  contentMarkdown: string
  publishedAt: string
  provenanceState: DesktopKnowledgeProvenanceState
  sourceMap: DesktopKnowledgeSourceMapEntry[]
}

export interface DesktopKnowledgeWorkingDraft {
  title: string
  contentMarkdown: string
  updatedAt: string
  provenanceState: DesktopKnowledgeProvenanceState
  sourceMap: DesktopKnowledgeSourceMapEntry[]
}

export interface DesktopKnowledgePage extends DesktopKnowledgePageSummary {
  materializedPath: string
  publishedRevision: DesktopKnowledgePublishedRevision | null
  workingDraft: DesktopKnowledgeWorkingDraft | null
  verification: DesktopKnowledgeVerificationStatus
  publicationDiagnostics: DesktopKnowledgePublicationDiagnostic[]
}

export interface DesktopKnowledgePages {
  pages: DesktopKnowledgePageSummary[]
  selectedPageId: string | null
}

export type DesktopKnowledgeAuthority = "generated" | "user"

interface DesktopKnowledgeWorkspaceItemSummaryBase {
  identity: string
  kind: DesktopKnowledgePageKind
  title: string
  updatedAt: string
  current: boolean
}

export type DesktopKnowledgeWorkspaceItemSummary =
  | (DesktopKnowledgeWorkspaceItemSummaryBase & {
      authority: "generated"
      generationId: number
      itemKey: string
      provenanceState: DesktopKnowledgeProvenanceState
    })
  | (DesktopKnowledgeWorkspaceItemSummaryBase & {
      authority: "user"
      pageId: string
      publicationState: DesktopKnowledgePagePublicationState
      lifecycleState: DesktopKnowledgeLifecycleState
    })

export type DesktopKnowledgeWorkspaceItemRequest =
  | {
      authority: "generated"
      generationId: number
      itemKey: string
    }
  | {
      authority: "user"
      pageId: string
    }

export interface DesktopKnowledgeWorkspace {
  currentGenerationId: number | null
  items: DesktopKnowledgeWorkspaceItemSummary[]
}

export interface DesktopGeneratedKnowledgeItem {
  authority: "generated"
  identity: string
  generationId: number
  itemKey: string
  kind: DesktopKnowledgePageKind
  title: string
  contentMarkdown: string
  entitySubtype: string | null
  aliases: string[]
  tags: string[]
  createdAt: string
  provenanceState: DesktopKnowledgeProvenanceState
  analysisProvenance: Record<string, unknown>
  sourceMap: DesktopKnowledgeSourceMapEntry[]
  current: boolean
  editable: false
}

export type DesktopUserKnowledgeItem = DesktopKnowledgePage & {
  authority: "user"
  identity: string
  editable: true
}

export type DesktopKnowledgeWorkspaceItem =
  | DesktopGeneratedKnowledgeItem
  | DesktopUserKnowledgeItem

export interface DesktopKnowledgeGenerationSummary {
  generationId: number
  parentGenerationId: number | null
  createdAt: string
  itemCount: number
  current: boolean
}

export type DesktopKnowledgeWorkspaceHistory =
  | {
      currentGenerationId: number | null
      generations: DesktopKnowledgeGenerationSummary[]
      generationId?: never
      items?: never
    }
  | {
      generationId: number
      parentGenerationId: number | null
      createdAt: string
      current: boolean
      items: DesktopKnowledgeWorkspaceItemSummary[]
      currentGenerationId?: never
      generations?: never
    }

export interface DesktopKnowledgeAdoptionCandidate {
  pageId: string
  title: string
  publicationState: DesktopKnowledgePagePublicationState
  match: "exact" | "possible"
  confidence: number
}

export type DesktopKnowledgeAdoptionDecision = "create_new" | "use_existing"

export interface DesktopKnowledgeAdoptionResult {
  status: "adopted" | "already_adopted" | "reconciliation_required" | "choice_required"
  generationId: number
  itemKey: string
  pageId: string | null
  origin: { generationId: number; itemKey: string }
  candidates: DesktopKnowledgeAdoptionCandidate[]
}

export interface DesktopKnowledgePageDeletion {
  pageId: string
  deleted: boolean
}
