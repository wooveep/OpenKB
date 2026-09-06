---
status: accepted
---

# Build the Knowledge Graph from admitted identities

The complete pipeline, state machine, migration policy, and acceptance matrix
are specified in the
[Knowledge Identity Graph pipeline design](../design/2026-09-04-openkb-knowledge-identity-graph-pipeline-design.md).

OpenKB builds its semantic graph only after Knowledge Analysis and Knowledge
Candidate Admission. Document PageTree remains the structural projection of a
Document Version—document, section, block, and EvidenceRef hierarchy—while the
Knowledge Identity Graph contains only canonical Concept, Entity, and Procedure
identities. Headings, prose claims, commands, paths, addresses, accounts,
configuration values, and procedure steps stay outside the identity layer. A
durable named and independently queryable component may be admitted as an Entity;
composition is then represented by an evidence-bound `PART_OF` edge.

Semantic Relation Analysis receives token-bounded batches that collectively
cover every admitted candidate claim. Each batch includes every claim-owning
candidate plus every admitted identity whose canonical title or alias occurs
literally in those claims. This is a lossless registry reduction under the same
endpoint-mention rule enforced by the output boundary: an identity that cannot
be named by the batch cannot participate in a locally valid assertion. The model
may emit only typed relations between supplied candidate IDs and must cite an
endpoint claim.

Routing is based on an explicit Candidate Registry Generation and document
provenance, never on whether admitted-candidate rows happen to exist. A current
analysis with zero admitted candidates publishes a completed-empty semantic
graph without a model call. A missing or failed candidate materialization blocks
semantic graph work, while only an explicitly marked pre-semantic document may
use the legacy evidence-graph adapter.

The boundary rejects invented identities, unknown claims, self-links, and
relation/end-point combinations outside the code-owned ontology. The initial
response is validated strictly. A response containing both valid and invalid
relations is accepted as an auditable degraded subset by the service because
each retained edge has independently passed identity, ontology, and evidence
validation. An all-invalid initial response receives one bounded repair; if that
repair is still all-invalid, the leaf completes as degraded-empty and records
every rejection rather than erasing valid relations from other batches.

Large-context models may use their advertised capacity, including DeepSeek Flash
profiles, but OpenKB still reserves prompt and output capacity, caps each request
at 64 claims, 64 eligible endpoint mentions, 64 returned relations, and four
minimal support claims per relation. An explicit provider output-limit failure
that includes final output is handled as a truncated structured result and
recursively splits the affected batch at a natural candidate boundary.
Reasoning-only output exhaustion remains a Model Result Failure under ADR-0042:
it invalidates the capability and never becomes a hidden split retry. No other
malformed response is hidden by splitting. If even one eligible registry plus
one claim cannot fit, the operation pauses explicitly instead of dropping a
prefix or suffix of the document.

Document relation assertions are mapped through the existing candidate-to-
identity resolution into the current Generated Knowledge Generation. Duplicate
document assertions collapse to one canonical typed edge while retaining source,
target, and assertion EvidenceRef bindings and applicability scopes. Graph-
augmented retrieval traverses that canonical generation for at most the existing
bounded hop count, then supplies only resolved Available EvidenceRefs to answer
generation. The model graph is therefore navigation metadata, never independent
answer authority.

Every graph task binds the Document Version and exact Candidate Registry
Generation that it analyzed. Its publish transaction rechecks both the durable
task claim and the current candidate generation; a reanalysis, cancellation, or
other superseding change makes a late model result ineligible for publication.
Retrieval never combines a stale relation generation with a newer identity
generation and degrades to its deterministic baseline while a compatible graph
is unavailable.

This replaces ADR 0004's direct Evidence Fragment-to-node extraction and ADR
0051's evidence-local graph candidate shape for documents with admitted Knowledge
Candidates. It preserves their evidence safety and graceful retrieval fallback,
as well as ADR 0067 and ADR 0068's separation between navigation guidance and
answer evidence. Legacy evidence graphs remain a compatibility fallback only for
documents that predate admitted candidates; migration schedules those documents
for semantic rebuilding rather than translating structurally plausible old nodes
into semantic identities.
