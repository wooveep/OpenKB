# Project authoritative knowledge as OKF and isolate PageTree providers

OpenKB keeps SQLite as the authority for knowledge, revisions, evidence, and
operational state while materializing an OKF-compatible Markdown projection
of approved Concepts, Entities, and current published generations for
progressive browsing and export. Source documents remain Raw Assets referenced
through OKF `sources`, rather than becoming duplicate Document or Evidence
pages. External OKF import and bidirectional Markdown editing remain deferred
because they introduce separate identity, provenance, and reconciliation
semantics. Projected files stay in fixed kind-based directories and use stable
page or generation identities, so title, description, tags, and logical topic
classification can change without changing an OKF Concept ID or breaking
links. Those metadata values may be proposed by Model Analysis and edited as a
User Revision. The projected `log.md` is rebuilt from retained revision,
generation, lifecycle, and Resolution Record metadata without preserving
discarded candidate content. Export offers an explicit knowledge-only form and
a self-contained form that additionally copies referenced Raw Assets.
Knowledge-only source resources use stable `urn:sha256:` identities plus an
OpenKB source manifest rather than absolute local paths; a self-contained
export rewrites them to relative paths inside the bundle.
Exports include Current Published Revisions in stable or deprecated lifecycle
states and exclude Working Drafts, review candidates, and discarded content.
Self-contained exports copy only referenced Raw Assets and Source Images.

Concept pages use OKF `type: Concept`; Entity pages use their normalized
subtype such as `Person` or `Organization` with `openkb.kind: Entity`, falling
back to `type: Entity` when no subtype is known. All OpenKB extension metadata
lives under one `openkb:` namespace. The projection emits ordinary relative
Markdown links and accepts both relative and OKF bundle-root links when reading.
Unmapped Knowledge Revisions remain exportable with
`openkb.provenance: legacy_unmapped` but gain no fabricated sources or
verification.
Standard Knowledge Links may add a low-weight, one-hop Catalog expansion, but
they remain untyped navigation and never become Knowledge Graph edges or Answer
Evidence.

Hierarchical retrieval enters through an OpenKB-owned PageTree Provider built
on Document IR and EvidenceRef contracts. A deterministic DocumentIR hierarchy
is built after evidence and is the base implementation; it stores structure,
locators, and EvidenceRef associations without another complete source copy.
PageTree Enrichment and an official PageIndex adapter remain optional. A
rebuildable Catalog Generation routes among published knowledge and source
documents, while an immutable Document PageTree is bound to one Document
Version and locates its sections and evidence. This split allows providers to
be evaluated and replaced without giving their parser, storage, or
answer-generation surfaces authority over the Desktop Knowledge Base.

Failure of either tree capability degrades to the existing FTS, Structure
Lexical Retrieval, and Wiki baseline without removing an otherwise valid
document from Available Knowledge. Deterministic retrieval runs first;
PageTree Selection is invoked only for long, complex, ambiguous, or poorly
covered questions. A Document PageTree may be reused across duplicate content
only when its structural IR fingerprint and locator mapping are also
identical. The official PageIndex Provider remains experimental until a fixed
evaluation proves long-document evidence gains without citation regression and
the packaged Windows runtime passes cold-start, size, timeout, and degradation
acceptance.

PageTree node summaries remain routing-only, and Table and Figure nodes retain
their EvidenceRef, locator, and Source Image associations. Provider output is
normalized into versioned SQLite tree generations; provider-private stores are
temporary, rebuildable caches rather than another authority. An official
PageIndex Provider becomes default only after the fixed local-fact, multi-hop,
cross-document-conflict, global-theme, and absent-answer evaluation shows at
least a 10% relative long-document Evidence Recall@6 gain, no more than a
one-point citation-precision or absent-answer regression, at most 10 seconds
additional retrieval p95, and at most one second additional Windows cold-start
p95.

PageTree Enrichment runs as a persisted, low-priority, recoverable capability
task after the document is available and remains separate from failed-document
handling. Query-time PageTree Selection examines at most three candidate
Document PageTrees in one Model Attempt with a 20-second deadline and no
automatic retry; timeout or failure immediately returns control to the
deterministic retrieval result.

The Import DAG checkpoints a non-blocking deterministic Document PageTree
after evidence and before Knowledge Analysis. A successful tree is published
with the document and supplies natural analysis batches; failure falls back to
ordered DocumentIR batching, does not quarantine the document, and schedules a
post-publication rebuild.

Knowledge Publication does not wait for its derived Catalog Generation. The
previous generation remains usable but stale while a persisted rebuild retries,
and direct SQLite, Wiki, and FTS paths see the new publication immediately.
Catalog failure cannot roll back the authoritative revision. PageTree trigger
thresholds remain an evaluation-driven internal policy whose reasons are
recorded in diagnostics rather than exposed as first-release settings.
Publication, deprecation or restoration, permanent deletion, Knowledge Source
Map changes, Document Availability changes, and successful Knowledge
Reanalysis invalidate the Catalog; Working Drafts, Conversations, and Answer
Versions do not.

Catalog Generations retain the current and most recent successful snapshot.
Each Document Version retains its current valid Document PageTree and keeps the
previous version only while rebuilding; older derived generations are removed
after active requests release them. Answer Versions retain Retrieval Trace IDs,
not the derived tree bodies.
