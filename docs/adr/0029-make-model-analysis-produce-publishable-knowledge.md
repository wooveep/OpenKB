# Make Model Analysis produce publishable knowledge

The persisted `model_analysis` Import Stage remains the mandatory, recoverable
pre-publication Knowledge Analysis boundary previously presented to users, but
it now validates and checkpoints a versioned structured result containing a
document description, Concept and Entity candidates, aliases, tags, and claims
with source Evidence IDs. An unparseable response remains a non-retryable format
failure that quarantines the document; a valid response may contain individual
unsupported claims, which become Missing Source Candidates without blocking
the remaining document publication. Graph extraction, PageTree Enrichment, and
answer generation stay outside this contract so their optional failures remain
independently degradable.

D0 and D1 imports reuse canonical source-backed analysis and resolve citations
through an available occurrence rather than paying for another Model Call. An
explicit Knowledge Reanalysis may produce a new generation under the current
model and prompt behavior; reuse is therefore the default, not an irreversible
freeze.

Long documents are analyzed as resumable Knowledge Analysis Batches aligned to
natural DocumentIR/PageTree sections, followed by a bounded document-level
merge. Completed batches are checkpointed and not rerun when a later batch or
merge call is retried. A Reanalysis produces candidates and passes through
Knowledge Reconciliation; failure leaves the current published knowledge and
Document Availability unchanged.

The checkpoint stores validated structured output, schema version, provider,
model, prompt digest, engine version, call metadata, and response hash rather
than another complete free-form response. OKF `generated.by` identifies the
stable OpenKB analysis producer and schema version, while provider-specific
details remain namespaced OpenKB metadata. Existing revisions and generations
without claim-level provenance become Unmapped Knowledge Revisions: they remain
browsable and may route queries, but they do not gain invented EvidenceRef
associations. Knowledge Reanalysis is the path to a valid Knowledge Source Map.

An analysis schema, prompt, or engine upgrade marks prior successful results as
Outdated Knowledge Analysis without invalidating their published knowledge.
The Documents and task surfaces offer explicit per-document and bulk Knowledge
Reanalysis; upgrades never trigger unapproved background model cost.

A schema-valid result with a usable document description but no Concept,
Entity, or source-backed claim is still a successful Knowledge Analysis. It
publishes the document for Evidence, FTS, Structure Lexical, and PageTree
retrieval without manufacturing knowledge merely to satisfy a non-empty output
expectation.

Publication is atomic: every Knowledge Analysis Batch and the Knowledge
Analysis Merge must validate before one publication transaction makes the
Document Version Available Knowledge. Failed, interrupted, or Awaiting Model
Configuration work is never partially searchable.
