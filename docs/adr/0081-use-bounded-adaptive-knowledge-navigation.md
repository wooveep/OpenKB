# Use bounded adaptive Knowledge Navigation

OpenKB keeps `DesktopEvidenceRetriever.retrieve(question) -> DesktopEvidencePack` as the only
production retrieval interface, but changes its implementation from one open-loop ranking pass
to one pinned, bounded Navigation Session. Deterministic vectorless retrieval remains the seed
and fallback. A simple supported factual lookup stops from that seed; a complex or unsupported
request may use the existing Analysis Model Gateway operation
`knowledge_navigation_step` to inspect current observations, report evidence-bound coverage,
and request a query-scoped route search for a missing aspect.

The model receives no file, SQL, path, or arbitrary tool capability. The structured contract
echoes the pinned Navigation Snapshot, keeps code-owned generic answer aspects, cites only
Evidence IDs already present in the session, and can request bounded semantic search terms,
advertised virtual routes, or logical sections anchored by already-known Evidence IDs. Coverage
claims silently drop unknown Evidence IDs and become `missing` when no valid binding remains;
action requests still reject invented Evidence, unadvertised routes, unknown or repeated actions,
unsafe path/SQL strings, and batches beyond the remaining budget. This distinction prevents a
harmless model binding typo from suspending the whole operation without granting it new authority.
One structured-output repair uses the existing operation suspension and explicit retry authority.
Optional failure preserves all verified deterministic Evidence and records `model_degraded`,
`snapshot_degraded`, `cancelled`, or `budget_exhausted` rather than claiming complete coverage.

Budgets are a session envelope, not fixed work quotas: at most three adaptive decisions, eight
physical model attempts across retrieval planning, PageTree selection, navigation and repair,
twenty-four logical reads, twenty-four thousand estimated source tokens, six navigation actions,
and 120 seconds checked between operations. Independent
knowledge and source reads remain batched. Later rounds exclude visited virtual routes, put new
gap-specific terms ahead of generic seed terms, and stop on no progress. Source expansion reads
small documents in full and otherwise preserves logical heading sections and whole blocks rather
than slicing a permanent 3,000-character window. Automatic whole-section expansion is reserved for
action-shaped questions or qualified procedure routes; a simple concept lookup stays on its exact
evidence unless a later validated action explicitly requests more. This keeps deprecated or
unrelated sibling facts in one physical source section from leaking into a simple lookup.

For a broad procedure chapter, source ordering exposes no more than eight shallow phases before
reserving role/scope, safety, validation, and safety-continuation checkpoints. It then returns the
remaining phase outline and detail, while deferring unrequested expansion, recovery, maintenance,
and upgrade branches. Source windows from different documents are round-robin fused. This retains
the original agent's useful summary-to-source navigation behavior without creating a second Wiki
store or raising the forty-EvidenceRef and source-token ceilings.

Evidence is merged by required answer aspect, with newly recovered aspect evidence ahead of
repetitive seed fragments, then deduplicated by canonical Evidence ID. Knowledge Guidance and an
ordered Answer Blueprint remain navigation-only; factual answer authority and citations remain
original Available Evidence. Retrieval Trace records safe objective metadata, coverage, rounds,
validated action kinds, budget usage, model calls, snapshot IDs, and the explicit stop reason,
without source excerpts or raw model output.

The virtual catalog persists typed `supported_by` and `references` relationships with both endpoint
routes, lifecycle eligibility, and the Evidence bindings that justify each edge. Structured SQLite
source bindings, not prose links, are authoritative for `supported_by`. Catalog, navigation, and
Portable Wiki export share one fail-closed inventory: a page is eligible only when it has at least
one binding and every bound Evidence occurrence belongs to an Available source document. The export
validator also rejects a knowledge page without a rendered Sources section. Unavailable or partially
unbound knowledge therefore cannot remain discoverable through a stale route or portable snapshot.

Release evidence is a digest-bound, aggregate-only real-corpus attestation. Version 2 binds the
current Python/Rust/TypeScript implementation digest and implementation commit, the frozen original
baseline commit and model profile, and the exact Windows portable payload. Its normalized package
digest excludes only the attestation itself, the release manifest, and local configuration, then
requires the actual payload inventory and declared manifest inventory to agree. It requires repeated
equivalent questions, fact-level completeness review, citation and correctness review, zero
unsupported material claims or degradation runs, restart replay, cancellation with the stable
`answer_cancelled` code, regeneration with preserved Answer Versions, structural corpus gates, the
packaged smoke test, and the automated regression suite. It stores neither private source text nor
generated answers.

This decision supersedes ADR 0069's fixed four-read/two-window allocation and ADR 0080's decision
to preserve those fixed limits and one-shot orchestration. It preserves ADR 0067's single virtual
Knowledge Navigation View, ADR 0068's separation of guidance from evidence, SQLite authority,
immutable Answer Versions, vectorless retrieval, and deterministic fallback. The original OpenKB
agent remains an offline behavioral baseline, not a runtime dependency.
