---
status: accepted
---

# Derive domain semantics under code-owned evidence constraints

OpenKB is a domain-neutral knowledge base: a model derives question facets and
knowledge-page structure from the actual question and source-backed corpus,
while code retains authority over evidence, identifiers, permissions, budgets,
snapshot consistency, validation, and safe rendering. Closed answer-kind
taxonomies, fixed role-to-section mappings, localized domain outlines, and
benchmark-shaped semantic rules are not production authority. When semantic
planning is unavailable or invalid, retrieval returns the verified baseline
Evidence Pack with semantic structure explicitly unknown; Grounded Answer
generation may continue, but Corpus Knowledge Synthesis retains the prior page
or omits a new one instead of publishing a fixed-template substitute.

Concept, Entity, and Procedure remain stable, code-owned identity kinds for
storage, lifecycle, and routing, but do not select page headings or answer
facets. Query-time Question Facet Plans and generation-bound Knowledge Page
Plans are separate contracts with shared structural primitives because their
lifecycles and authorities differ. A model names dynamic facets or sections and
references only supplied claim and Evidence identities; it does not return
unrestricted Markdown. Runtime acceptance validates structure, identifiers,
evidence bindings, safe labels, and completeness, while semantic suitability is
an offline evaluation concern rather than a second model-judge call. Domain
adaptation is evidence-driven by default. Runtime `AGENTS.md` prompt overrides
remain prohibited, while any future declarative guidance must be validated,
versioned, and included in the Prompt Contract digest. Semantic headings use
the Knowledge Page Language and come from the model, not code-owned translation
tables.

The Question Facet Plan contains one short goal and ordered, plan-local dynamic
facets marked required or supporting; fixed answer kinds and dedicated semantic
slots are removed, while search terms remain solely in the Retrieval Plan. The
Knowledge Page Plan permits an optional unheaded lead and a section tree of at
most two levels, and must place every eligible supplied claim exactly once.
Claim roles cease to be a code-owned enum or eligibility input, with no new
claim-level semantic tags until a concrete consumer and evaluated benefit exist.
Rendering initially supports only `paragraph`, `unordered_list`, and
`ordered_list`. The current cosmetic `table` form is removed and may return only
through a future contract whose headers, rows, and cells have atomic evidence-
bound inputs.

Models do not author plan-local facet, section, or unit identifiers. They return
ordered semantic structures with Initial Answer Coverage inline per facet and
references only to supplied Evidence, claim, or related-identity IDs. After
validation, code derives stable local identifiers from the canonical order and
normalized content. Dynamic text receives Unicode normalization, control-
character and newline rejection, length bounds, and boundary-specific Markdown
escaping; URL fragments and domain vocabulary are not rejected by semantic
regular expressions. All source-derived text remains explicitly delimited as
untrusted data in later prompts and receives no tool authority.

One physical `query_planning` Model Call returns independently validated
Retrieval Plan and Question Facet Plan results after deterministic seed
retrieval. It receives the question, bounded Conversation Context, and bounded,
ID-labelled seed observations treated as untrusted source material. The accepted
semantic branch includes the Question Facet Plan and a complete Initial Answer
Coverage over the supplied seed IDs. They are accepted together only when every
facet has one valid state and every support binding resolves; one targeted repair
may fix the branch, after which failure yields Unknown Semantic Structure while
the independent Retrieval Plan remains usable. An accepted facet plan remains
immutable for its Navigation Session; later navigation may update coverage but
cannot add, delete, or rewrite facets. Coverage has only `covered`, `partial`,
and `missing` facet states. Retrieval Trace retains the canonical accepted facet
plan, digest, Prompt Contract identity, and Model Execution Profile, but never
raw model output or rationale.

Knowledge Page Planning is a separate plan-only operation after claim
consolidation. It may batch small identities for efficiency, but validates each
Knowledge Page Plan independently. An invalid item receives at most one targeted
repair and then becomes a Deferred Knowledge Cluster, carrying its prior item
forward or omitting a new item without blocking valid siblings. Each accepted
plan persists with its generation, identity, claim-snapshot digest, Prompt
Contract, and planner provenance. Plans govern Generated Knowledge Items only.
An adopted User Knowledge Page preserves its origin and source bindings while
allowing user-authored organization under the ordinary Publication Gate.
The planner may optionally place supplied, evidence-bound Semantic Relation
Assertion IDs alongside claims, but relation placement is not exhaustive and
there is no mandatory or code-named related-knowledge section.

Navigation always begins with deterministic seed retrieval. Only a missing or
partial required facet may authorize an adaptive read; supporting facets never
force expansion. Covered required facets, no progress, and budget exhaustion
are terminal, while absence of a valid facet plan returns the verified seed
without question-type regexes or automatic answer-shape expansion. Page
rendering emits the self-contained evidence-bound claim prose produced by
Corpus Knowledge Synthesis and adds only planned headings, list markers, and
source markers; the planner cannot generate introductions or connective prose.
The Grounded Answer prompt receives only the dynamic goal, ordered facets,
coverage, and bound source Evidence. It selects natural prose without an answer-
kind template, covers every supported required facet, and discloses partial or
missing required facets instead of filling them from model memory.

Corpus Knowledge Synthesis merges equivalent claim text and applicability before
the immutable planner snapshot and accumulates every supporting Evidence ID.
Duplicate claims that survive into the snapshot fail Semantic Plan Integrity;
the renderer emits exactly the validated claim placement and performs no silent
semantic deduplication.

Claim Applicability Scope is an open list of model-labelled dimension/value
entries, each bound to a subset of its claim's Evidence IDs. Deterministic
within-generation normalization may merge equivalent labels; uncertain
cross-document equivalence enters review. Document Summary semantic units are
likewise dynamically labelled and evidence-bound, and no longer use the fixed
`purpose`, `applicability`, or `key_topic` role taxonomy as production authority.

Knowledge Candidate Admission follows the same authority split. The model
proposes candidates and their Concept, Entity, or Procedure kind from supplied
Evidence; optional identity labels are open metadata. Code validates Evidence
ownership, identity collisions, safe bounded text, and resource limits but does
not apply a finite subtype ontology or vocabulary-shaped rejection rules for
paths, URLs, commands, configuration values, or other domain literals. Exact
canonical claim text and applicability may be merged deterministically with all
supporting Evidence. Non-literal equivalence, complement, conflict, or position
is a model-produced, evidence-bound semantic judgment, with unresolved cases
entering review rather than being inferred from roles, polarity tokens, or
keywords.

Evidence Binding Integrity proves that supplied identifiers resolve within the
pinned lineage, snapshot, and authority boundary; it does not prove that the
Evidence semantically entails a generated claim. Semantic Support remains a
live-model evaluation and human-review concern, and user-facing language must
not describe structural source binding as deterministic factual verification.
The Knowledge Identity Graph follows the same boundary: relation labels are
bounded model output, while code validates endpoints and supporting claim IDs.
The fixed relationship enum and label-specific endpoint/ranking rules are
removed, and navigation traverses relationships as bidirectional adjacency for
discovery regardless of their directed display semantics. Relation assertions
merge Evidence only for identical directed endpoints and exactly normalized
labels; semantically similar or reverse-direction labels remain separate. When
model relation analysis is unavailable, the graph channel contributes no newly
generated relationship semantics and direct Evidence retrieval remains the safe
fallback; code does not synthesize a keyword- or pattern-derived semantic graph.

Production Prompt Contracts and validators remain provider-neutral and use the
Model Provider Adapter for capability encoding; they contain no DeepSeek-specific
business branch. Query Planning is an Analysis Model operation on the reserved
Interactive Model Lane and degrades only the current query when unavailable.
Knowledge Page Planning is an Analysis Model operation under background Analysis
Concurrency and defers only the affected identity. Grounded Answer generation
continues to use the independently configured Answer Model. The fixed DeepSeek
selection below belongs only to the repository's release-evaluation profile.

The technical-operations corpus remains one regression suite inside a
heterogeneous evaluation matrix rather than the source of universal production
semantics. Runtime activation uses only a deterministic Corpus Generation
Integrity Gate; semantic appropriateness is tested offline through technical
operations, natural-science explanation, person or historical narrative,
literary or argumentative analysis, and non-IT procedural suites, including at
least one structurally equivalent Chinese/English pair. This narrows ADR 0055
to making Procedure an available identity kind rather than a default semantic
frame, supersedes ADR 0059's fixed kind-derived page organization, supersedes
ADR 0065's use of a real-corpus benchmark as a candidate activation gate, and
supersedes ADR 0081 only where it assigns generic answer aspects or answer-shape
classification to code. Their evidence, bounded-execution, snapshot, and
deterministic-baseline guarantees remain in force.

Ordinary CI exercises deterministic validators, contract fixtures, and
metamorphic invariants. Candidate releases run every case in the Semantic
Quality Evaluation Matrix three times against a pinned Live Evaluation Profile;
deterministic invariants must hold on every run, and a human-authored semantic
rubric must pass separately for every domain suite rather than by aggregate
average. Results are recorded in the release attestation. Evaluation credentials
may be loaded from the current repository's local, Git-ignored `.env`, but
secret values never enter prompts, logs, fixtures, artifacts, attestations, or
version control. Ordinary development runs explicitly skip live evaluation when
`LLM_API_KEY` is absent; the candidate-release command fails with an actionable
message.

Evaluation corpora, questions, human rubrics, and metamorphic mappings live in
a dedicated evaluation-only tree that runtime packages cannot import. The
repository tracks the non-secret profile and expected evaluation metadata;
complete live outputs remain ignored local artifacts, while attestations retain
only digests, the pinned profile, aggregate results, and human verdicts. A live
run cannot mark itself passed: the release attestation remains pending until a
maintainer explicitly signs the human-authored rubric verdict, binding the
rubric, output and profile digests, maintainer identity, and decision time.

The default non-secret live-evaluation endpoint is
`https://api.deepseek.com` and the default model identifier is
`deepseek-v4-flash`; both were verified against the DeepSeek API documentation
when this decision was recorded. The gating profile explicitly disables thinking
and uses the provider's `json_object` Structured Output Mode with code-owned
closed validation and one repair; an optional high-thinking profile is
diagnostic only. This profile is evaluation-only and does not change Desktop
Knowledge Base Model Configuration or establish a product-default provider.

Resource and safety limits remain code-owned contract data rather than semantic
policy. One contract definition supplies its prompt description, output schema,
and validator with initial maxima of twelve question facets, two section levels,
thirty-two sections, sixty-four eligible claims per identity, eighty characters
per label, and four hundred characters per facet description. Adjustments
require cross-domain evaluation and budget evidence, not a single corpus fixture.

## Relationship to Spec #100

This ADR narrowly supersedes Spec #100 where that umbrella specification assigns
semantic authority to a code-owned Admission Policy, finite subtype or relation
ontologies, fixed dossier purposes, keyword-based identity rejection, semantic
generation qualification, a legacy graph adapter, or backward-compatible
migration of the replaced semantic contracts. It also replaces the fixed
answer-kind and aspect internals inherited from #99 while retaining the bounded
Navigator and single retrieval interface.

Spec #100 remains authoritative for Document Lineage, Version Catalog and Diff,
Version Scope, Evidence and Evidence Occurrence authority, Citation
Postconditions, immutable candidate and generated generations, snapshot
pinning, task supersession, atomic activation, User Knowledge Page ownership,
privacy, locks, and the rule that maintenance operations never initiate model
work. Its OCloudView corpus remains a regression suite, not universal production
semantics or an automatic runtime activation gate.

This is a clean contract replacement. No migration, compatibility parser, or
legacy-tag preservation is required for answer-kind, claim-role, dossier-purpose,
presentation, or semantic-plan payloads created by the superseded implementation.
An obsolete development Knowledge Base is rejected with an actionable request
to recreate and reimport it; the application never silently deletes or rewrites
its data. The cutover raises the Knowledge Base schema epoch and atomically
replaces old operation names, dossier tables and fields, relation enums,
answer-kind coverage fields, Retrieval Trace JSON, and Rust/frontend wire
contracts. There are no old aliases or permissive readers in the new runtime.
Version-scope `not_applicable` remains a distinct version-routing state and is
not confused with removed facet coverage.
