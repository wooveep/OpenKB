type LoadCurrentKnowledgeWorkspace = (
  search: string,
  preferredPageId?: string,
  requestedSequence?: number,
) => Promise<"loaded" | "preferred_missing" | "stale">

export function knowledgeWorkspaceRequestIsCurrent(
  requestSequence: number,
  currentRequestSequence: number,
): boolean {
  return requestSequence === currentRequestSequence
}

/** Preserve a changed page when possible, then fall back after deletion or filtering. */
export async function reloadKnowledgeWorkspaceAfterUserMutation(
  loadCurrent: LoadCurrentKnowledgeWorkspace,
  query: string,
  preferredPageId: string | null,
  requestSequence: number,
): Promise<void> {
  if (preferredPageId) {
    const outcome = await loadCurrent(query, preferredPageId, requestSequence)
    if (outcome !== "preferred_missing") return
  }
  await loadCurrent(query, undefined, requestSequence)
}
