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
advertised virtual routes, or logical sections anchored by already-known Evidence IDs. Code
rejects stale snapshots, invented Evidence, unadvertised routes, unknown or repeated actions,
unsafe path/SQL strings, and batches beyond the remaining budget. One structured-output repair uses
the existing operation suspension and explicit retry authority. Optional failure preserves all
verified deterministic Evidence and records `model_degraded`, `snapshot_degraded`, `cancelled`,
or `budget_exhausted` rather than claiming complete coverage.

Budgets are a session envelope, not fixed work quotas: at most three adaptive decisions, eight
physical model attempts across retrieval planning, PageTree selection, navigation and repair,
twenty-four logical reads, twenty-four thousand estimated source tokens, six navigation actions,
and 120 seconds checked between operations. Independent
knowledge and source reads remain batched. Later rounds exclude visited virtual routes, put new
gap-specific terms ahead of generic seed terms, and stop on no progress. Source expansion reads
small documents in full and otherwise preserves logical heading sections and whole blocks rather
than slicing a permanent 3,000-character window.

Evidence is merged by required answer aspect, with newly recovered aspect evidence ahead of
repetitive seed fragments, then deduplicated by canonical Evidence ID. Knowledge Guidance and an
ordered Answer Blueprint remain navigation-only; factual answer authority and citations remain
original Available Evidence. Retrieval Trace records safe objective metadata, coverage, rounds,
validated action kinds, budget usage, model calls, snapshot IDs, and the explicit stop reason,
without source excerpts or raw model output.

This decision supersedes ADR 0069's fixed four-read/two-window allocation and ADR 0080's decision
to preserve those fixed limits and one-shot orchestration. It preserves ADR 0067's single virtual
Knowledge Navigation View, ADR 0068's separation of guidance from evidence, SQLite authority,
immutable Answer Versions, vectorless retrieval, and deterministic fallback. The original OpenKB
agent remains an offline behavioral baseline, not a runtime dependency.
