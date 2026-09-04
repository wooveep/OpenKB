# OpenKB

OpenKB turns user-supplied documents into a local knowledge base and exposes
grounded retrieval and generation surfaces over that knowledge.

## Language

**Desktop Workbench**:
The Windows desktop application through which one local user manages a
knowledge base, imports documents, inspects processing, and asks questions.
_Avoid_: Web Workbench, GUI

**Desktop Knowledge Base**:
A new-format knowledge base created by the Desktop Workbench and backed by the
new SQLite, Raw Asset, and staged-processing model.
_Avoid_: legacy knowledge base

**Active Knowledge Base**:
The one Desktop Knowledge Base currently bound to a Desktop Runtime for
interactive reads, writes, imports, and answers.
_Avoid_: open knowledge bases, workspace collection

**Legacy Knowledge Base**:
A knowledge base created by the previous CLI/Web workflow with Markdown files
as its primary state.
_Avoid_: Desktop Knowledge Base

**Desktop Runtime**:
The user-managed application lifetime that hosts the Desktop Workbench and its
managed child workers without requiring a separately managed local backend
service. All child processes end with the application.
_Avoid_: backend service, single OS process

**Desktop Shell**:
The Tauri-hosted application boundary that owns windows and native system
integration, presents the React workbench, and supervises the Python Engine.
_Avoid_: Web server, business backend

**Python Engine**:
The packaged child runtime that executes OpenKB application services and
background knowledge work under Desktop Shell supervision.
_Avoid_: standalone service, user-managed backend, sidecar

**Startup Readiness**:
The visible two-phase state in which the Desktop Workbench is available before
the Python Engine is ready for Engine-backed operations.
_Avoid_: fully started application

**Desktop Bridge**:
The single versioned command, query, error, and event contract through which
the React workbench reaches the Python Engine under Desktop Shell mediation.
_Avoid_: REST client, direct sidecar call, component IPC

**Knowledge Workspace Contract**:
The additive Desktop Bridge surface for listing and reading current Generated
Knowledge Items and User Knowledge Pages, adopting generated items, and reading
generation history. Existing knowledge-page operations retain their meaning for
User Knowledge Pages only.
_Avoid_: redefined Knowledge Page contract, generated-only API

**Portable Desktop Package**:
A self-contained Windows distribution that a user extracts and starts through
one executable entry point without installing language runtimes or services.
_Avoid_: installer, development environment

**Diagnostic Bundle**:
A user-reviewed, explicitly exported support artifact containing sanitized
runtime metadata and logs but no source content, Prompt Contract snapshots,
model outputs, raw reasoning, or credentials.
_Avoid_: telemetry, automatic crash report

**Application Log**:
An automatically maintained, rotating, support-shareable diagnostic record for
the Desktop Shell and Python Engine, kept in application state rather than in a
Desktop Knowledge Base or the program directory. It excludes source content,
Prompt Contract snapshots, model outputs, raw reasoning, credentials, and other
unsanitized payloads at every verbosity level.
_Avoid_: console output, Diagnostic Bundle, Sensitive Trace Capture

**Sensitive Trace Capture**:
An explicitly authorized, time-bounded local diagnostic artifact, separate from
Application Logs and Diagnostic Bundles, that may retain unredacted paths,
exceptions, prompts, failed model requests and responses, raw reasoning, and
other sensitive failure evidence. It is never an automatic support export.
_Avoid_: Application Log, Diagnostic Bundle, permanent trace archive

**Portable Local Configuration**:
The optional user-authored, application-wide configuration beside a Portable
Desktop Package's executable. It is the sole mutable program-directory runtime
file, is outside the signed release inventory, and applies before any Desktop
Knowledge Base is opened.
_Avoid_: Knowledge Base configuration, release manifest, Application Log

**Diagnostic Event**:
A correlated, support-safe record of a meaningful runtime boundary, state
transition, decision, or terminal result. Repeated query snapshots, streaming
chunks, and document-content units are not Diagnostic Events.
_Avoid_: log line, telemetry, model payload

**Diagnostic Component**:
The stable Desktop Runtime subsystem label used for per-component verbosity and
failure ownership: `shell`, `bridge`, `runtime`, `import`, `parser`, `model`,
`page_tree`, `retrieval`, `knowledge`, `projection`, or `storage`.
_Avoid_: Python module name, logger hierarchy, process name

**Failure Attribution**:
The support-safe classification attached to a terminal Diagnostic Event that
identifies its pipeline stage, execution phase, responsible component, failure
kind and code, retryability, outcome, and correlation identities. It preserves
the distinction among Network Failure, Provider Failure, and Model Result
Failure instead of collapsing them into a generic model-response problem.
_Avoid_: error message, stack trace, model response problem

**Failure Context**:
The bounded, self-contained diagnostic snapshot attached to a terminal failure
so the default Application Log can explain the last completed stage, failing
phase, relevant safe observations, and next action without lower-verbosity log
history.
_Avoid_: preceding debug trace, exception dump, model payload

**Failure Owner**:
The Diagnostic Component with enough causal context to emit the one canonical
terminal event for a failure. Upstream boundaries reference that event instead
of restating the same failure.
_Avoid_: every exception handler, Engine request boundary

**Runtime Restoration**:
The reopening of the previously Active Knowledge Base and its recoverable work
when a new Desktop Runtime starts.
_Avoid_: silent knowledge-base creation, system reboot recovery

**Import Job**:
A user-initiated, recoverable processing run that brings one or more source
documents into a knowledge base and reports the state of each processing
stage.
_Avoid_: upload

**Interrupted Import Job**:
An Import Job stopped by user cancellation or application shutdown while its
completed Stage Runs and Knowledge Analysis Batches remain recoverable. It is
not quarantined and resumes only through an explicit user action.
_Avoid_: failed import, automatic startup resume

**Awaiting Model Configuration**:
The recoverable Import Job state reached after usable DocumentIR is available
but mandatory Knowledge Analysis cannot start because its Model Execution
Profile is missing, unavailable, or has not passed its explicit Model
Capability Check. The document remains unpublished until explicit resume
succeeds.
_Avoid_: Quarantined Document, Available Knowledge

**Import Batch**:
A set of Import Jobs submitted together for progress tracking, not a boundary
for answer availability.
_Avoid_: publication unit

**Document IR**:
The format-neutral, ordered block representation produced by a Parser Adapter
before evidence, search, PageTree, knowledge, or graph processing. Its blocks
retain headings, paragraphs, lists, code, tables, figures, assets, and source
coordinates such as page, slide, sheet, cell range, and bounding box.
_Avoid_: converted Markdown

**OKF Knowledge Projection**:
The rebuildable, OKF-compatible Markdown representation of authoritative
SQLite knowledge, intended for progressive browsing and export but not as an
independently editable or importable source of truth.
_Avoid_: OKF database, bidirectional Markdown store

**OKF Concept ID**:
The stable identity of one projected knowledge artifact, derived from its
immutable User Knowledge Page or generation identity rather than its editable
title.
_Avoid_: page title, regenerated slug

**Knowledge Projection Export**:
An exported OKF Bundle containing published knowledge and a source manifest
without copying the referenced Raw Assets.
_Avoid_: backup, self-contained bundle

**Self-contained Knowledge Bundle**:
An exported OKF Bundle that includes the Raw Assets referenced by its published
knowledge so the source links remain usable outside the original knowledge base.
_Avoid_: knowledge-only export

**PageTree Provider**:
The replaceable boundary that derives a hierarchical retrieval tree from one
Document IR snapshot and resolves selected nodes back to EvidenceRefs.
_Avoid_: PageIndex Chat, document parser

**Catalog Tree**:
The rebuildable corpus-level hierarchy that routes a question among published
Concepts, Entities, and their source documents without containing answer
evidence itself.
_Avoid_: global document PageTree, Knowledge Graph

**PageTree Selection**:
A bounded, optional model decision that chooses relevant Document PageTree
nodes when deterministic retrieval cannot confidently cover a question.
_Avoid_: mandatory query step, answer generation

**PageTree Enrichment**:
Optional node summaries added after a deterministic Document PageTree exists;
their absence or failure does not change Document Availability.
_Avoid_: base tree construction, mandatory import stage

**Document PageTree**:
The immutable hierarchy for one Document Version that routes a question to
source sections and EvidenceRefs derived from its Document IR.
_Avoid_: Catalog Tree, heading keyword score

**Catalog Generation**:
One immutable Catalog Tree snapshot derived from a consistent set of published
knowledge, Knowledge Metadata, sources, and document availability.
_Avoid_: application-start scan, Document PageTree

**Structure Lexical Retrieval**:
The deterministic retrieval channel that scores source headings and text
without performing tree traversal or model reasoning.
_Avoid_: PageTree retrieval, PageIndex

**Parser Adapter**:
A format-specific converter that reads one Raw Asset and emits validated
Document IR plus independently stored Source Images and parser diagnostics.
Parsing and evidence chunking are separate stages.
_Avoid_: universal converter

**Parser Readiness Check**:
A lightweight startup verification that required packaged parser assets are
present without loading heavyweight parser engines. It reports Parser Runtime
State independently from import and model status.
_Avoid_: parser prewarm, parser startup

**Parser Runtime State**:
The observable lifecycle of a heavyweight parser capability: `resources_ready`,
`not_loaded`, `initializing`, `ready`, or `unavailable`, with a stable diagnostic
code when unavailable.
_Avoid_: Knowledge Analysis status, model timeout

**DocumentIR Usability Gate**:
The deterministic validation of extracted text quantity and quality, source
locators, and structural integrity before evidence construction or any Model
Call. An insufficient fast result selects an enhanced route when available;
an insufficient final result requires manual recovery.
_Avoid_: LLM quality check, non-empty string check

**Enhanced PDF Parsing**:
The bundled CPU path that adds OCR, document-layout recognition, and table
structure recognition when fast text extraction is empty, garbled, or sparse
on at least half of the pages. It is a deterministic parser capability, not an
Embedding or generative model.
_Avoid_: automatic LLM analysis

**Parser Route Override**:
A manual recovery choice that forces the fast or enhanced parser route for one
Import Job without changing the knowledge base default. Enhanced DOCX/PPTX
recovery may OCR embedded images when direct document text is insufficient.
_Avoid_: global parser setting, automatic OCR of every image

**Legacy Office Compatibility**:
Best-effort plain-text and metadata extraction for binary `.doc` and `.ppt`
assets through packaged python-tika, a private Tika Server JAR, and a bundled
Java runtime. Preflight may initialize that runtime alongside Raw Asset work;
once loaded it is reused until Engine exit. It does not promise images, tables,
pages, slides, or layout.
_Avoid_: high-fidelity Office parsing

**Document Version**:
An immutable imported representation of a source document, distinguished from
other versions by its content.
_Avoid_: document update

**Document Summary**:
A Document Version-bound navigation artifact whose deterministic section map is
derived from DocumentIR and whose optional semantic purpose, applicability, and
key-content units each bind to Evidence. New analysis produces those semantic
units inside the existing Knowledge Analysis batch-and-merge operation rather
than through another Model Call. Invalid semantic units degrade to the section
map without blocking otherwise valid Document Knowledge Candidates or Available
Knowledge. It is neither a corpus Knowledge Identity nor a User Knowledge Page.
_Avoid_: Generated Knowledge Item, Concept, query-time summary

**Document Version Candidate**:
A possible new Document Version identified by semantic similarity and awaiting
user confirmation of which source document, if any, it belongs to.
_Avoid_: automatic version

**D0 Asset Duplicate**:
An imported asset whose raw bytes have the same content hash as an existing
asset.

**Raw Asset**:
The sole retained full-byte copy of an imported source document, stored under
`raw/` and integrity-referenced by SQLite.
_Avoid_: CAS source copy

**D1 Text Duplicate**:
An imported document whose normalized body has the same content hash as an
existing Document Version.

**D2 Evidence Duplicate**:
A page or evidence fragment whose normalized content already exists and can be
reused without counting as independent support.

**D3 Near-Duplicate Candidate**:
Semantically similar content that may overlap an existing document or evidence
item and therefore requires a prompt or Review Queue decision.
_Avoid_: automatic duplicate

**Model Configuration**:
The Desktop Knowledge Base-scoped API Base URL, API Key, default model, and
optional role-specific model selections and capability overrides used for its
Model Calls. It contains no model response-timeout setting.
_Avoid_: environment configuration, credential reference

**Effective Model Role Settings**:
The resolved model, context capacity, reasoning mode, and adapter constraints
after default-role inheritance. Model Configuration shows both selected and
effective values so absence, inheritance, and provider default remain distinct.
_Avoid_: saved fields, Model Execution Profile

**Custom Model Provider**:
A user-supplied model endpoint without a code-owned Model Provider Adapter. It
may serve as an Answer Model after a successful streaming capability check but
cannot serve as an Analysis Model because OpenKB cannot guarantee its structured
output, reasoning controls, or stream semantics.
_Avoid_: named model provider, arbitrary provider parameters

**Model Provider Adapter**:
The code-owned protocol contract for one named provider, including structured-
output modes and request encoding, reasoning-control encoding, streaming fields,
finish reasons, and capability version. OpenKB never selects one by guessing
from a Custom Model Provider's model name.
_Avoid_: Model Capability Profile, raw provider options

**Analysis Model**:
The optional Model Configuration selection for `knowledge_analysis`,
`knowledge_analysis_batch`, `knowledge_analysis_merge`,
`page_tree_enrichment`, `knowledge_graph_extraction`, and `retrieval_plan`;
when absent, those structured operations use the default model.
_Avoid_: analysis provider, extraction credential

**Analysis Concurrency**:
The Desktop Knowledge Base limit on simultaneously active background Analysis
Model Attempts. It defaults to two and is adjustable from one through four as
an advanced Model Configuration setting; interactive retrieval planning uses a
reserved Interactive Model Lane instead.
_Avoid_: unbounded parallelism, import batch size

**Answer Model**:
The optional Model Configuration selection used only for `grounded_answer`;
when absent, Grounded Answer generation uses the default model.
_Avoid_: chat provider, answer credential

**Model Capability Profile**:
The provider- and model-aware context capacity, supported Structured Output
Modes, streaming semantics, and reasoning-control format resolved from known
metadata and advanced user overrides. Unknown models use a 16K-token context
assumption, and unsupported controls are omitted.
_Avoid_: Prompt Contract, provider promise

**Model Execution Profile**:
The immutable, fully resolved Model Provider Adapter and version, provider,
model, exact Structured Output Mode, streaming controls, reasoning mode, and
token budgets used by one Knowledge Analysis Plan. Recovery uses the pinned
profile rather than current Model Configuration.
_Avoid_: Model Configuration, Model Capability Profile

**Structured Output Mode**:
The exact provider request strategy pinned for one structured Analysis Model
Call: native `json_schema`, native `json_object`, or a Prompt Contract without
native response formatting. It never applies to `grounded_answer`, whose Answer
Model result remains streamed natural language. A Model Provider Adapter selects
the strongest mode it explicitly supports and never changes mode after a failed
call.
_Avoid_: local schema validation, silent provider fallback

**Structured Analysis Reasoning**:
The provider-side reasoning mode used by structured Analysis Model operations.
It resolves from the explicit Analysis setting, then an explicit default-role
setting, and finally `off`; `low`, `medium`, or `high` opts in through the Model
Provider Adapter's supported control format.
_Avoid_: provider-default reasoning, Prompt Contract reasoning

**Reasoning Token Allowance**:
The bounded generation capacity added beside the final structured-output
reserve when Structured Analysis Reasoning is enabled: `low`, `medium`, and
`high` receive `0.5×`, `1×`, and `2×` the final reserve. The selected level is
never silently reduced when the model context cannot contain it.
_Avoid_: final output reserve, unlimited reasoning budget

**Model Capability Check**:
A cancellable, explicitly initiated check cached for one complete Model
Execution Profile. A small bounded request exercises its actual streaming,
reasoning control, finish reason, final content, and schema behavior; generated
check content is not persisted.
_Avoid_: TCP probe, document analysis, health telemetry

**Save and Verify Model Configuration**:
The explicit user action that persists Model Configuration and then runs its
required role-specific Model Capability Checks after disclosing that provider
calls may incur cost. A failed or cancelled check does not roll back the saved
configuration. Stop prevents only role checks that have not yet been dispatched;
an already-dispatched check finishes and retains its result.
_Avoid_: automatic verification, hidden provider call, save-only readiness

**Model Role Verification**:
The independently recorded verified, failed, cancelled, unverified, unavailable,
or not-required readiness of one configured model role. Analysis and Answer are
required; a distinct Default is not checked, while an inherited Default is
explicitly covered by Answer. Results may be reused only when the complete role
and capability-check signature is identical, not merely the provider or model.
_Avoid_: global model readiness, shared-model shortcut

**Model Capability Check Cache**:
The knowledge-base-local successful check for one exact Model Execution
Profile. It has no time expiry but is invalidated by profile or adapter changes,
by a failed capability check, or by a confirmed shared adapter-protocol breach;
an uncertain breach requires the same signature across two independent
operations, while schema and domain-validation failures remain operation-local.
_Avoid_: permanent provider guarantee, global capability registry

**Model Operation Contract State**:
The knowledge-base-local readiness of one model operation under an exact Model
Execution Profile and Prompt Contract. An operation-specific invalid result may
suspend that exact combination; only an explicit retry reopens it unchanged,
while a profile or contract change starts a new unverified state without an
automatic provider call. Regenerate Answer, Recover Import, and explicit
Start/Retry Knowledge Reanalysis are the user actions that can reopen their
respective suspended contracts. Their action-scoped retry round may contain
parallel batches, but ends on a validated/failed terminal result or when the
action stops; ordinary new work cannot reuse it. Structured-output repair uses
a contract bound to its parent operation, parent Prompt Contract, and schema.
_Avoid_: Model Capability Check Cache, global model failure

**Legacy Capability Invalidation**:
An unverified Model Execution Profile state created before operation-specific
failure scoping. Migration carries forward only an exact durable row that was
still recorded as verified; unchecked or invalidated history remains unverified,
even when a nearby legacy diagnostic appears operation-local.
_Avoid_: automatic verification, discarded failure history

**Interactive Model Lane**:
At least one high-priority model execution slot reserved for an interactive
`retrieval_plan` and `grounded_answer` pipeline so background imports cannot
starve questions over already Available Knowledge.
_Avoid_: background Analysis Concurrency, unlimited answer concurrency

**Prompt Contract**:
The code-owned, versioned combination of model instructions, input shape,
output schema, validation rules, and bounded generation policy for one operation.
Every structured contract explicitly asks for JSON and carries a code-owned
canonical JSON output example accepted by both its schema and operation
validator. Knowledge Analysis Plans retain the complete canonical snapshot and
digest for recovery.
_Avoid_: AGENTS.md prompt, user prompt override

**Structured Output Repair**:
The single Analysis Model call allowed after deterministic normalization and
local schema validation cannot make a structured result operation-valid. It
receives the original schema, its canonical JSON example, the validation errors,
and evidence-bound source material; an empty final result, Reasoning Output
Exhaustion, or a second invalid result ends automatic recovery.
_Avoid_: transport retry, unbounded self-correction

**Model Call**:
A logical request for one OpenKB result from a configured model, potentially
fulfilled by several Model Attempts. It has no OpenKB-imposed total, read,
thinking, or generation deadline; elapsed time alone never ends it.
_Avoid_: API request

**Model Call Status**:
The task-history summary of a Model Call or Model Attempt: `running`,
`retry_wait`, `completed`, or `failed`. Detailed lifecycle states and failure
causes never replace this summary value.
_Avoid_: Model Call Lifecycle Status, Provider Failure

**Model Call Lifecycle Status**:
The detailed event state of a live or completed Model Attempt, including queue,
connection, response, validation, retry, cancellation, and terminal failure
states. It may explain a Model Call Status but is not interchangeable with it.
_Avoid_: task status, Import Job status

**Model Attempt**:
One provider request issued in pursuit of a Model Call. It ends only with a
valid provider response, an explicit Provider Failure, a Network Failure, a
Model Result Failure, user cancellation, or application shutdown. Cancellation
is best effort and does not guarantee that a provider without cancellation
support stops computing.
_Avoid_: retry

**Model Retry Policy**:
At most three Model Attempts for one Model Call, with `Retry-After` or bounded
backoff between attempts, and no enclosing time budget. Only explicit transient
Provider Failures and Network Failures are retryable; authentication, input,
permission, and other permanent failures stop immediately.
_Avoid_: deadline retry, retry forever

**Awaiting Model Result**:
The nonterminal Model Attempt state after request dispatch and before any
compatible reasoning or final-content response activity or a terminal provider,
transport, cancellation, or shutdown event. It means OpenKB is still waiting
and does not claim that the provider is thinking or making progress. The UI
reports elapsed wait, batch progress, attempt count, and a cancellation action.
_Avoid_: model thinking, hung model, model timeout

**Model Output Activity**:
An observed final-content chunk from a streaming Model Attempt. Structured
output is buffered for final validation while the UI may report that output is
arriving; raw chunks are not displayed or persisted, and activity never
controls timeout.
_Avoid_: reasoning progress, validated result, heartbeat deadline

**Reasoning Output Activity**:
An observed provider-labeled reasoning chunk from a streaming Model Attempt,
reported only as sanitized activity while its Model Call Status remains
`running`. It proves transport activity, not valid output or semantic progress;
raw reasoning is never displayed, persisted, or used as the result.
_Avoid_: Model Output Activity, model thinking, chain-of-thought display

**Reasoning Output Exhaustion**:
A Model Result Failure in which the provider reaches its output limit after
emitting reasoning but before returning final content. OpenKB never uses raw
reasoning as the result or silently retries with different reasoning settings.
_Avoid_: Provider Failure, invalid structured output, model timeout

**Long Wait Advisory**:
A nonterminal notice shown after a Model Attempt has waited longer than the
greater of five minutes or twice that role/model's local historical P95. It
offers cancellation but never fails or retries the attempt.
_Avoid_: warning timeout, model deadline

**Model Usage Record**:
The local per-call record of role, model, IDs, batch, queue/connect/first-output
and total timing, classified result, call count, and provider-reported tokens.
It also retains finish reason and content-free reasoning/final-output presence,
chunk-count, and character-count metadata. Missing token counts are visibly
estimated, and currency is shown only when the user configures pricing.
_Avoid_: source telemetry, inferred provider bill

**Model Result Failure**:
A non-retryable terminal response for any Model Call role in which the provider
request succeeds but does not yield a usable final model result. Its specific
failure code explains whether output was absent, contained reasoning without a
final result, exhausted its budget in reasoning, was truncated, or remained
structurally invalid after permitted repair.
_Avoid_: Provider Failure, Network Failure, completed Model Call

**Provider Failure**:
An explicit error returned by the model API, including a provider-declared
timeout such as an HTTP 408 or 504. It ends the current Model Attempt but is not
an OpenKB response timeout.
_Avoid_: model deadline exceeded

**Network Failure**:
A failure to complete DNS/TCP/TLS connection establishment within 30 seconds,
a connection loss, or another explicit transport error. Once the request is
sent, OpenKB applies no first-byte, read, or total-response timeout; mere silence
while awaiting a result is not a Network Failure.
_Avoid_: model response timeout

**Knowledge Analysis**:
The mandatory, versioned Model Analysis result that describes one imported
document as source-backed Document Knowledge Candidates before that Document
Version may be published.
_Avoid_: provider connection test, graph extraction, PageTree Enrichment

**Concept**:
A reusable explanatory idea, principle, mechanism, or category whose identity
comes from its meaning rather than from a named concrete object or an ordered
operational goal.
_Avoid_: Entity, Procedure, document heading

**Entity**:
A durable, named, independently queryable object that is reusable across claims,
such as a product, component, service, organization, or formally recurring tool.
A script, path, address, account, log, package filename, or configuration value
remains part of a claim unless it is itself such an object.
_Avoid_: every noun phrase, raw literal, Procedure

**Procedure**:
A reusable, goal-oriented unit of operational knowledge with an applicability
scope, prerequisites, ordered actions, and an observable completion condition.
Commands, paths, individual steps, and troubleshooting observations are claims
within a Procedure rather than separate knowledge identities.
_Avoid_: Concept, task heading, command page

**Document Knowledge Candidate**:
A document-scoped, evidence-bound Concept, Entity, or Procedure hypothesis
produced by Knowledge Analysis before corpus-level identity resolution and
synthesis.
_Avoid_: Generated Knowledge Item, Knowledge Graph Candidate

**Knowledge Candidate Admission**:
The gate that admits a Document Knowledge Candidate to corpus consolidation only
when it is independently queryable, reusable beyond one local passage, and
supported by evidence containing a definition, goal, or substantive claim.
Document titles, table-of-contents entries, revision records, isolated literals,
and one-off parameter values remain metadata or claims rather than identities.
_Avoid_: extract every heading, delayed noise cleanup, Entity Resolution

**Candidate Registry Generation**:
The explicit, document-scoped dependency snapshot created whenever one validated
Knowledge Analysis is materialized. It has a stable generation identity, the
complete candidate-and-claim digest, admission counts, and Evidence bindings. An
empty current generation means analysis succeeded with no admitted identities;
an absent generation means the dependency is unavailable. Semantic Relation
Analysis and publication remain bound to the exact current generation.
_Avoid_: candidate row count, implicit legacy marker, Generated Knowledge Generation

**Knowledge Identity**:
The corpus-stable identity for one reusable Concept or Entity subject, or for one
user-completable Procedure goal. Supporting subtopics become sections; an
operation with an independently useful goal or lifecycle receives its own
Procedure identity and may be referenced by a broader Procedure.
_Avoid_: source section, topic keyword, whole-product omnibus page

**Knowledge Identity Graph**:
The rebuildable corpus graph whose nodes are admitted, corpus-stable Knowledge
Identities and whose typed edges are evidence-bound Semantic Relation
Assertions. It links Concepts, Entities, and Procedures without turning source
headings, claims, commands, paths, parameters, or PageTree nodes into semantic
identities. Query-time traversal resolves every selected node and edge back to
original Available EvidenceRefs before answer generation.
_Avoid_: Document PageTree, evidence-fragment graph, heading graph

**Semantic Relation Assertion**:
A typed, directed relationship between two admitted Document Knowledge
Candidates, supported by one or more claims belonging to an endpoint and by the
EvidenceRefs for those claims. Relation analysis may select existing candidate
IDs but may not create, rename, merge, or reclassify identities. Corpus
consolidation maps both endpoints to Knowledge Identities before publication.
_Avoid_: free-text predicate, model-created node, unsupported co-occurrence

**Component Entity**:
A durable, named component that is independently queryable and therefore earns
an Entity identity; its membership in another Entity is expressed by a
`PART_OF` Semantic Relation Assertion. An unnamed part, field, step, command,
path, or parameter remains a claim about its owner rather than a graph node.
_Avoid_: every part noun, section nesting, Procedure step

**Canonical Knowledge Title**:
The stable display title for a Knowledge Identity, chosen from an evidence-backed
official full name or, when none exists, the dominant corpus form. Deterministic
case, spacing, punctuation, full-name, and supported abbreviation variants are
Knowledge Aliases; an action phrase is not an alias for an Entity.
_Avoid_: first occurrence, model-invented label, source heading

**Knowledge Alias**:
An evidence-backed or deterministically derived naming variant that resolves to
one Knowledge Identity without changing its kind or semantic boundary.
_Avoid_: related topic, action on an Entity, unverified model synonym

**Knowledge Analysis Batch**:
A recoverable, token-budgeted analysis checkpoint for one group of natural
DocumentIR sections. Its validated result is reused when the containing
Knowledge Analysis resumes.
_Avoid_: Import Batch, whole-document retry

**Knowledge Analysis Plan**:
The immutable execution manifest created when a Knowledge Analysis starts. It
pins the DocumentIR digest, Model Execution Profile, exact Structured Output
Mode, Prompt Contract including its JSON example, final structured-output
reserve, bounded reasoning allowance, natural-section batch boundaries, and
merge topology so recovery does not mix incompatible results.
For a model with unknown capacity, it assumes a 16K-token context and allows
approximately 8K tokens of document input per batch, reserving the remainder
for instructions and output.
_Avoid_: latest model settings, mutable batch queue

**Knowledge Analysis Replan**:
The explicit recovery that supersedes a legacy or incompatible Knowledge
Analysis Plan within the same Import Job. It preserves deterministic Raw Asset,
DocumentIR, and Evidence work but never reuses analysis batches or merge
checkpoints from the superseded plan.
_Avoid_: plan mutation, document reimport, mixed-plan recovery

**Knowledge Analysis Merge**:
The checkpointed document-level reduction of completed Knowledge Analysis
Batches. It deterministically combines identical candidate identities, aliases,
tags, claims, and evidence links, then uses token-bounded hierarchical Model
Calls only for document summaries and unresolved conflicts.
_Avoid_: one giant merge prompt, concatenation

**Knowledge Candidate Consolidation**:
The evidence-preserving resolution of Document Knowledge Candidates into
high-confidence generated identities; ambiguous identity matches remain review
work rather than being merged automatically.
_Avoid_: Knowledge Analysis Merge, Knowledge Reconciliation

**Semantic Relation Analysis**:
The post-admission, document-scoped Model Analysis that inspects every
evidence-bound claim in token-bounded batches and emits only relationships among
the supplied candidate IDs. Each batch carries all claim owners plus admitted
identities named literally by a canonical title or alias in those claims; the
same eligibility rule is enforced at the output boundary. Content is split or
explicitly paused when it cannot fit, never silently prefix-truncated. An
explicit output-limit stop after final output recursively splits only the
affected claim batch; reasoning-only exhaustion is a Model Result Failure, not
batch compression. Invalid endpoints, incompatible type pairs, or unsupported
claim references are rejected and recorded at the model boundary, while
independently valid edges remain usable as a degraded result.
_Avoid_: identity extraction, PageTree construction, first-N evidence scan

**Automatic Knowledge Identity Match**:
A same-kind consolidation decision supported by an identical normalized title
or verified Knowledge Alias, or by at least two independent non-model identity
signals plus a confirming structured Model Result and no contrary evidence.
Cross-kind, model-only, and contradicted matches enter the Review Queue.
_Avoid_: title substring match, model confidence alone, cross-kind merge

**Corpus Knowledge Synthesis**:
The bounded integration of consolidated, source-backed generated claims across
documents into readable corpus-level Generated Knowledge Items. It never
rewrites User Knowledge Pages automatically.
_Avoid_: Knowledge Analysis, Knowledge Graph extraction, claim concatenation

**Incremental Corpus Synthesis**:
The debounced automatic synthesis of only the Knowledge Identities affected by
newly available document candidates. The document becomes Available Knowledge
before synthesis finishes, and the prior Generated Knowledge Generation remains
current until the complete candidate generation validates and activates.
Knowledge Reanalysis remains explicit.
_Avoid_: blocking document publication, per-document activation, automatic reanalysis

**Corpus Synthesis Budget**:
The configured Model Call and token ceilings for one Corpus Knowledge Synthesis
run after deterministic admission and clustering. Estimated and actual usage are
visible; reaching a ceiling pauses the resumable candidate generation without
truncating content or displacing the current generation.
_Avoid_: silent topic dropping, unlimited calls, partial activation

**Corpus Synthesis Batch**:
A token-bounded Structured Output request that synthesizes several compatible
small Knowledge Identities while preserving an independent result and source map
for each. An oversized Procedure receives its own batch; validation isolates
cross-item leakage or failure so only the affected item is retried.
_Avoid_: one Model Call per page, whole-corpus prompt, shared page prose

**Claim Applicability Scope**:
The product version, platform, deployment scenario, and time boundary within
which a source-backed knowledge claim applies, derived only from explicit
evidence or reliable document metadata. Claims from distinct scopes may coexist;
materially incompatible claims in the same scope require review rather than a
newest-wins choice.
_Avoid_: source document identity, global latest value, Conflict

**Unspecified Applicability**:
The explicit state of a Claim Applicability Scope dimension that its evidence
does not establish. It is neither a wildcard nor a value inherited from document
recency or neighboring claims; a conflict with an explicitly scoped claim enters
review.
_Avoid_: universal applicability, inferred latest version, missing data error

**Generated Knowledge Generation**:
An immutable corpus-wide snapshot of Generated Knowledge Items produced by one
successful Corpus Knowledge Synthesis. It becomes current only through atomic
activation; an incomplete or failed synthesis leaves the prior generation
current and does not affect document availability.
_Avoid_: Knowledge Graph Generation, per-document activation, Working Draft

**Deferred Knowledge Cluster**:
An affected identity cluster withheld from new synthesis because its identity or
claims require review. A new generation carries forward its prior Generated
Knowledge Item when one exists, or omits a new item while retaining its source
Evidence for direct retrieval; it does not block safe clusters from activating.
_Avoid_: duplicate provisional pages, failed generation, discarded evidence

**Corpus Reanalysis Run**:
An explicit user-initiated run that reanalyzes selected existing Document
Versions and combines their new candidates with unchanged corpus candidates to
build one candidate Generated Knowledge Generation. The run activates that
generation only after every selected analysis and every non-deferred
consolidation, synthesis, and validation step succeeds. Starting the run after
its preview authorizes that atomic activation.
_Avoid_: automatic migration, per-document activation, destructive rebuild

**Corpus Reanalysis Preview**:
The pre-run disclosure of selected Document Versions, estimated affected
Knowledge Identities, Model Calls, token budget, and recoverable snapshot that
lets the user authorize one Corpus Reanalysis Run.
_Avoid_: post-run approval, silent model work, destructive migration

**Corpus Reanalysis Change Report**:
The post-run comparison that counts filtered candidates, consolidated identities,
new and changed items, carried-forward items, Deferred Knowledge Clusters, and
review work while retaining the prior Generated Knowledge Generation for
rollback.
_Avoid_: model transcript, second activation prompt, irreversible replacement

**Corpus Knowledge Quality Gate**:
The fixed real-corpus benchmark and regression contract a candidate Generated
Knowledge Generation must pass before activation. It measures evidence binding,
candidate noise, duplicate identities, multi-document synthesis, Procedure
coverage, representative Grounded Answers, and existing retrieval behavior.
_Avoid_: page-count comparison, average length, subjective spot check

**Outdated Knowledge Analysis**:
A still-usable Knowledge Analysis produced by an older analysis schema, prompt,
or engine version and eligible for user-initiated Knowledge Reanalysis.
_Avoid_: failed analysis, automatic migration

**Knowledge Reanalysis**:
An explicit regeneration of Knowledge Analysis for an already imported
Document Version using current model behavior without reimporting its Raw Asset.
_Avoid_: duplicate import, automatic D1 analysis

**Unmapped Knowledge Revision**:
An existing User Knowledge Page revision that predates the Knowledge Source Map
and may support browsing and routing but not claim-level evidence selection.
_Avoid_: Missing Source Candidate, invalid migration

**Stage Run**:
An independently tracked execution of one named processing stage within an
Import Job.
_Avoid_: task

**Import Progress**:
The ordered status of preflight, Raw Asset, parser initialization, DocumentIR,
Evidence, Knowledge Analysis Plan, batches, merge, and publication Stage Runs.
Batch progress is based only on completed checkpoints; elapsed time never
creates a synthetic completion percentage.
_Avoid_: estimated percent, generic analyzing state

**Conversation**:
A persisted, ordered exchange of user questions and Grounded Answers within
one Desktop Knowledge Base.
_Avoid_: flat answer history, chat card list

**Conversation Context**:
The bounded set of earlier completed exchanges used to interpret a follow-up
question without replacing fresh retrieval from Available Knowledge.
_Avoid_: Answer Evidence, full transcript prompt

**Grounded Answer**:
An immutable answer assembled from completed knowledge with inspectable source
evidence. Regeneration creates another Answer Version rather than rewriting it.
_Avoid_: AI response, chat result

**Interrupted Answer**:
A partial streamed response retained in a conversation with a visible stopped
state after its Model Call fails, including when the provider emits only
reasoning and no final content, until a successful Answer Retry replaces it.
_Avoid_: completed answer

**Answer Retry**:
An explicit user-initiated renewed Model Call for an Interrupted Answer that
replaces that answer in place only once a completed response is available.
_Avoid_: duplicate message

**Answer Version**:
One completed alternative for an assistant message in a Conversation. Earlier
versions remain selectable without creating a separate conversation branch.
_Avoid_: duplicate answer, conversation branch

**Answer Evidence**:
The EvidenceRefs and original Source Images associated with a Grounded Answer
that make its support inspectable. It contains only evidence supplied to the
answer model for that Grounded Answer.
_Avoid_: decorative media

**Evidence Drawer**:
The on-demand view of Answer Evidence for one selected Grounded Answer, opened
without displacing the Conversation message stream.
_Avoid_: inline source list, permanent inspector

**Source Image**:
An original document image saved independently at import time and eligible for
direct presentation as Answer Evidence.
_Avoid_: generated image

**Available Knowledge**:
Non-quarantined Document Versions whose DocumentIR, Evidence, and Document
Knowledge Candidates have validated and are currently eligible for Grounded
Answers, even while other Import Jobs or Corpus Knowledge Synthesis runs remain
active.
_Avoid_: complete batch

**Retrieval Plan**:
A validated, bounded set of lexical and semantic search terms derived from a
question for the retrieval channels. It is a discovery signal, not the
representation of answer intent or scope. Failure to create a model-assisted
plan does not disable deterministic baseline retrieval.
_Avoid_: generated search query

**Navigation Objective**:
The answer kind, subject, requested scope, named entities, Concepts, requested
actions, constraints, and required answer aspects pinned and refined inside one
Navigation Session. It remains navigation metadata and never supports a fact.
_Avoid_: Retrieval Plan, answer outline, model rationale

**Navigation Session**:
The private, bounded observe-plan-read loop behind one
`DesktopEvidenceRetriever.retrieve` call. It owns the pinned Navigation
Snapshot, Navigation Objective, visited actions and routes, Answer Coverage,
budget use, Evidence allocation, degradation, and explicit stop outcome.
_Avoid_: Wiki Agent, Conversation, second retrieval path

**Knowledge Navigation View**:
A query-time, virtual read model over one Navigation Snapshot of the Catalog,
Document Summaries, current knowledge pages, and bounded source sections. It is
not the Knowledge Workspace, an authority store, or a live Wiki directory.
_Avoid_: Knowledge Workspace, OKF projection, filesystem mirror

**Navigation Snapshot**:
The immutable Knowledge Snapshot Revision, Generated Knowledge Generation,
available-document set, and derived Catalog and PageTree generation identities
pinned for one Grounded Answer's navigation.
_Avoid_: latest state per read, Generated Knowledge Generation alone

**Vectorless Semantic Retrieval**:
Semantic candidate discovery without Embedding models or vector indexes. It
combines an optional Retrieval Plan with PageTree reasoning, FTS5, Knowledge
Pages, entity aliases, and evidence-backed graph traversal.
_Avoid_: vector search

**Evidence Pack**:
The source-only, generation-consistent set of ranked EvidenceRefs, source
coordinates, Source Images, channel reasons, and degradation state within one
Grounding Context. It contains no generated knowledge prose, unpublished graph
candidates, or Quarantined Document results.
_Avoid_: Grounding Context, Knowledge Guidance, prompt context

**Knowledge Guidance**:
The source-bound units read from current generated or published user knowledge
that organize an answer but never constitute Answer Evidence or an independently
citable fact source. Eligibility is evaluated per factual unit: a unit whose
Knowledge Source Bindings do not all resolve to Available Evidence is omitted,
while valid sibling units and complementary generated knowledge remain usable.
_Avoid_: Evidence Pack, Answer Evidence, unbound generated prose

**Grounding Context**:
The Navigation Snapshot, Knowledge Guidance, and source-only Evidence Pack
provided together for one answer generation.
_Avoid_: Evidence Pack, answer prompt, Conversation Context

**Grounding Budget**:
The model-capacity-aware input and output envelope enforced for one Grounded
Answer. It reserves the Prompt Contract's output allowance, bounds Conversation
Context, applies the configured document-input share, gives source Evidence at
least three quarters of that document budget, and limits Knowledge Guidance to
the remainder. An unknown model capacity selects a conservative code-owned
fallback rather than an unbounded prompt.
_Avoid_: Evidence Pack character limit, provider default, user-tuned prompt

**Navigation Read Budget**:
The code-owned session envelope over navigation rounds, physical Model Calls,
total logical reads, source input tokens, wall time, and search expansion. Its
values are maxima rather than fixed per-kind allocations; independent reads may
be batched, and the Answer Coverage Gate stops work as soon as scope is covered.
_Avoid_: model turn limit, target read count, open-ended agent loop

**Navigation Evidence Envelope**:
The model-capacity-aware ceiling on unique Available EvidenceRefs and source
tokens retained while Navigation merges its reads. Small or unknown Answer
models use the conservative baseline; verified large-context models receive a
larger bounded envelope. It never changes the evidence-only authority rule and
does not require filling the available context.
_Avoid_: fixed first-N citation cap, provider output ceiling, Knowledge Guidance

**Answer Coverage Gate**:
The evidence-bound comparison of a Navigation Objective's required aspects with
collected Available Evidence. Every aspect is covered, partial, missing, or not
applicable and binds canonical Evidence IDs when supported. Knowledge Guidance
alone never satisfies coverage; meaningful gaps may authorize another bounded
navigation round and remaining gaps are disclosed by aspect.
_Avoid_: Corpus Knowledge Quality Gate, guidance-unit count, open-ended research

**Answer Blueprint**:
The ordered, navigation-only projection of Answer Coverage and its canonical
Evidence IDs passed to the answer model. It organizes synthesis but cannot add a
fact or replace Original Evidence citations.
_Avoid_: generated answer, Knowledge Guidance, chain of thought

**Evidence Occurrence Postcondition**:
The deterministic final-answer check that preserves the positional meaning of
canonical Evidence repeated after multiple source steps. When the answer cites a
step but omits that step's repeated occurrence, OpenKB restores the exact
Evidence excerpt and citation at that position; it never generates a substitute
claim or repairs a step the answer did not cite.
_Avoid_: corpus-specific answer patch, semantic inference, uncited completion

**Retrieval Trace**:
The compact source-content-free record of the Navigation Objective summary,
Navigation Snapshot, selected channels, rounds, validated action kinds, route
IDs, Answer Coverage, budget use, model cost, stop outcome, and degradation that
explains how an Answer Version obtained its immutable Answer Evidence.
_Avoid_: model chain of thought, regenerated search

**Entity Resolution Candidate**:
A possible identity match generated from normalized names, aliases, types,
lexical similarity, and optional bounded LLM judgment. A D3 candidate requires
review and never merges entities automatically.
_Avoid_: resolved entity

**Knowledge Graph Candidate**:
A proposed node or relationship extracted from model output that has not yet
satisfied the Knowledge Graph's evidence and relationship constraints.
_Avoid_: graph record, validated graph

**Knowledge Graph**:
An internal, evidence-backed relationship structure used to improve retrieval
and Grounded Answers; it is not a first-release Desktop Workbench browsing
surface.
_Avoid_: graph view

**Canonical Graph Relationship Type**:
A code-owned stable relationship category used for graph storage and retrieval
across model and provider variations.
_Avoid_: Graph Relation Label, arbitrary predicate

**Graph Relation Label**:
A bounded source-facing relationship phrase retained from extraction without
becoming the stable graph query category.
_Avoid_: Canonical Graph Relationship Type

**Graph Support Quote**:
A bounded verbatim substring of one EvidenceRef used to prove that a Knowledge
Graph Candidate is anchored in source text without proving its interpretation.
_Avoid_: generated explanation, semantic verification

**Graph Support Locator**:
The EvidenceRef-relative character range derived from a locally validated Graph
Support Quote so graph records can return to source without duplicating its text.
_Avoid_: stored quote copy, document-global offset

**Empty Knowledge Graph Result**:
A successful Knowledge Graph extraction whose valid model result explicitly
contains no candidates. It is not produced by rejecting every candidate from a
nonempty invalid result.
_Avoid_: invalid graph response, rejected candidate set, failed extraction

**Degraded Knowledge Graph Result**:
A completed Knowledge Graph extraction that publishes only its evidence-safe
subset while explicitly reporting weakened or rejected candidates.
_Avoid_: Empty Knowledge Graph Result, failed extraction, silent partial success

**Knowledge Graph Result Quality**:
The `full` or `degraded` assessment of a completed Knowledge Graph result,
recorded independently from whether its extraction lifecycle completed or failed.
_Avoid_: extraction status, diagnostic log level

**Current Knowledge Graph Generation**:
The immutable Knowledge Graph result selected for retrieval by compatibility and
quality; a compatible full generation is not displaced by a degraded attempt.
_Avoid_: latest extraction attempt, merged graph attempts

**Legacy Evidence-bound Graph Record**:
A migrated graph node or edge that retains its valid EvidenceRef but predates
Graph Support Locator verification.
_Avoid_: quote-verified record, automatically reanalysed record

**Knowledge Graph Issue**:
A source-content-free classification of one rejected, weakened, or fatal graph
candidate condition, including its contract path and disposition.
_Avoid_: model response excerpt, generic graph failure

**Graph Verification State**:
The locally derived `source_anchored`, `ambiguous`, or `legacy_evidence_bound`
assessment of a retained graph record; model-reported confidence is not authoritative.
_Avoid_: support score, provider confidence

**Knowledge Graph Compatibility Key**:
The Document Version and Evidence snapshot, canonical graph schema, normalizer,
and verification-policy versions that determine whether a full generation may remain current.
_Avoid_: provider identity, model identity, Evidence ID alone

**Knowledge Graph Failure Signature**:
A source-content-free structural failure identity used across independent documents
to distinguish a shared operation-contract problem from one document's semantic failure.
_Avoid_: exception text, source excerpt, single-document failure

**Knowledge Workspace**:
The unified browsing surface for Generated Knowledge Items and User Knowledge
Pages; its default scope contains the current generated generation and all user
pages, while prior generated generations remain explicit history.
_Avoid_: Knowledge Navigation View, User Knowledge Page, generated-only list

**Portable Wiki Export**:
An explicit export mode that uses the existing transactional export pipeline to
materialize one Navigation Snapshot as semantic-route Markdown for portable
browsing or external tools. It contains a root index, Document Summaries,
published User Knowledge Pages, the current Generated Knowledge Generation,
full Available Document source views, referenced images, and a Portable Wiki
Manifest, but no Raw Assets. It neither participates in runtime retrieval nor
changes or continuously mirrors the Knowledge Base.
_Avoid_: Knowledge Navigation View, live Wiki directory, authority backup

**Portable Wiki Manifest**:
The versioned inventory of a Portable Wiki Export's Navigation Snapshot, routes,
route aliases, resources, and checksums. All routes, links, and checksums must
validate before the staged export is atomically published to a new immutable
directory.
_Avoid_: source manifest, database backup, mutable export state

**Portable Source View**:
The complete Markdown projection of one Available Document Version in a
Portable Wiki Export, stored as one semantic-slug file with stable heading,
page, and source-locator anchors and relative Source Image links. It is derived
from DocumentIR and is not split into per-page files or treated as a Raw Asset.
_Avoid_: Evidence-only excerpt, original binary, runtime Source Read Window

**Knowledge Base Compatibility Migration**:
An additive, transactional schema transition preceded by a restorable backup.
It exposes existing generated knowledge as a Legacy Unqualified Knowledge
Generation, derives safe structural routes and summaries in place, and never
triggers Model Calls, Knowledge Reanalysis, or authoritative-content rewriting.
_Avoid_: document reimport, destructive upgrade

**Legacy Unqualified Knowledge Generation**:
A compatibility-preserved generation created from knowledge produced before the
current candidate admission, corpus synthesis, and quality contracts. It remains
browsable, exportable through existing OKF compatibility surfaces, and available
for rollback, but cannot supply Knowledge Guidance until explicit Corpus
Reanalysis produces a generation that passes the current quality gate.
_Avoid_: failed generation, automatically trusted legacy knowledge, deleted history

**Generated Knowledge Item**:
An immutable, generation-bound corpus-level Concept, Entity, or Procedure
produced by Corpus Knowledge Synthesis and browsable as generated knowledge. It
remains read-only unless a user adopts it into a separate User Knowledge Page.
_Avoid_: User Knowledge Page, Working Draft

**Knowledge Page Content Contract**:
The kind-specific readable structure for a Generated Knowledge Item or User
Knowledge Page. Entity pages explain identity, role, capabilities, applicability,
related operations, and limitations; Concept pages explain definition,
mechanism, scope, tradeoffs, and relations; Procedure pages explain goal, scope,
prerequisites, ordered steps, validation, rollback, and troubleshooting. Empty
sections are omitted, equivalent claims become one unit with all supporting
sources, complementary claims form coherent prose, scoped differences remain
visible, and every factual paragraph or list item retains claim-level sources.
_Avoid_: universal prose template, raw claim dump, page-level citation only

**Knowledge Page Language**:
The Knowledge Base preference used consistently for synthesized explanatory
prose, defaulted from the corpus's dominant language and explicitly overridable.
Official names, commands, paths, and other exact technical literals retain their
source form, and a query's language does not rewrite stored pages.
_Avoid_: per-source mixed prose, query-time page rewrite, forced translation of literals

**User Knowledge Page**:
A user-owned Concept, Entity, or Procedure with stable identity, revisions, and
an explicit publication lifecycle.
_Avoid_: Generated Knowledge Item, source document

**Knowledge Adoption**:
The explicit creation of a User Knowledge Page Working Draft from a Generated
Knowledge Item, or the explicit routing of that item to an existing page's
Review Queue, while preserving its source map and generation provenance. It
does not edit or replace the originating generated item or silently overwrite
the selected page.
_Avoid_: edit generated knowledge, automatic promotion

**Knowledge Origin Reference**:
The immutable `(generation_id, item_key)` link from an adopted User Knowledge
Page to the Generated Knowledge Item that seeded it; the page receives its own
stable identity.
_Avoid_: User Knowledge Page ID, title match

**Current Published Revision**:
The one User Knowledge Page revision currently eligible for navigation and,
through its source bindings, retrieval; it remains active while a newer draft
is edited.
_Avoid_: latest saved draft, generation candidate

**Working Draft**:
The recoverable, unpublished revision being edited for one User Knowledge Page,
which may coexist with its Current Published Revision.
_Avoid_: Current Published Revision, temporary editor text

**Knowledge Publication**:
The explicit user action that atomically promotes a Working Draft which passes
the Publication Gate to become the Current Published Revision.
_Avoid_: autosave, verification

**Knowledge Verification**:
An explicit human confirmation of one complete User Knowledge Page revision and
its sources. Any subsequent content, source, or lifecycle change requires a
new verification.
_Avoid_: save, conflict selection

**Knowledge Trust Tier**:
The advisory unverified, machine-confirmed, or human-reviewed state derived from
Knowledge Verification and used only as a routing tie-breaker.
_Avoid_: access control, evidence relevance

**Knowledge Link**:
A standard Markdown link between projected knowledge pages that may expand
Catalog routing by one hop but asserts no relationship type or factual support.
_Avoid_: TypedEdge, EvidenceRef

**Knowledge Lifecycle**:
The draft, stable, deprecated, or stale condition of published knowledge,
independent of source-document availability and import processing state.
_Avoid_: Document Availability, quarantine status

**Knowledge Metadata**:
The editable title, description, tags, lifecycle, and source associations of a
User Knowledge Page revision, none of which determines the page's stable
identity.
_Avoid_: Concept ID, generated taxonomy

**Draft Revision**:
A saved User Knowledge Page revision that is not eligible for Grounded Answers
because it has not passed the Publication Gate.
_Avoid_: failed save, published revision

**OKF Compatibility Lint**:
The permissive validation that checks an OKF Knowledge Projection against the
OKF format while tolerating optional fields, extensions, and broken links.
_Avoid_: Publication Gate

**Publication Gate**:
OpenKB's stronger eligibility check requiring resolvable available evidence,
permitted Knowledge Lifecycle, and a generation-consistent snapshot before
knowledge may contribute to Grounded Answers.
_Avoid_: OKF format validation

**Knowledge Change Log**:
The rebuildable OKF history of revision, generation, lifecycle, and resolution
events that excludes content deleted by a submitted conflict decision.
_Avoid_: rejected candidate archive

**Source-backed Knowledge Claim**:
A statement in a Generated Knowledge Item or User Knowledge Page whose OKF
source marker resolves to an EvidenceRef in Available Knowledge. It may route
and rank source evidence but is never itself Answer Evidence.
_Avoid_: unsourced user note, generated assertion

**Knowledge Source Map**:
The revision- or generation-scoped mapping from OKF source identifiers in
knowledge Markdown to EvidenceRefs in Available Knowledge.
_Avoid_: document-level provenance, projected footnote list

**Knowledge Source ID**:
A stable, page-local OKF footnote identity derived from a canonical EvidenceRef
and preserved across source reordering and later revisions.
_Avoid_: source array index, random footnote ID

**Knowledge Source Binding**:
The user-visible association of one claim-level Markdown unit with one or more
document sections from Available Knowledge, represented portably as OKF source
markers. Its compact marker opens the supporting document, section, page or
coordinate, and original excerpt; repeated sources share stable page-local IDs
and appear once in the page's source summary.
_Avoid_: manual Evidence ID entry, page-level source

**Missing Source Candidate**:
A model-generated knowledge claim that cannot resolve to supporting evidence
and remains outside published knowledge until a user binds a source or discards
it.
_Avoid_: Quarantined Document, stable knowledge

**User Revision**:
A user-authored revision of a User Knowledge Page stored in authority data and
materialized to Markdown.
_Avoid_: Markdown edit

**Conflict**:
An incompatible incoming change to approved knowledge that cannot be merged
automatically with an existing revision.
_Avoid_: import failure

**Knowledge Reconciliation**:
Classification of incoming Concept, Entity, and Procedure changes as compatible
additions or Conflicts against approved knowledge, with Claim Applicability
Scope distinguishing legitimate variants from same-scope contradictions.
Adoption never overwrites a possible matching User Knowledge Page: a confident
match enters reconciliation, while an ambiguous match requires the user to
choose the existing page or a new page.
_Avoid_: document deduplication

**Three-way Knowledge Reconciliation**:
Review of Current Published Revision, Working Draft, and incoming knowledge
when an import affects a page with unpublished user work.
_Avoid_: automatic draft merge, document version review

**Knowledge Deprecation**:
The reversible lifecycle change that removes a published User Knowledge Page
from default routing while retaining its identity, content, and history.
_Avoid_: permanent deletion

**Review Queue**:
A persisted, evidence-comparative collection of typed identity and claim
problems. Identity actions include merge, keep separate, set alias, change kind,
and exclude noise; claim actions include set applicability, preserve scoped
variants, adopt a correction, and exclude an invalid claim. Batch resolution is
available only for items sharing the same deterministic reason.
_Avoid_: exception module

**Resolution Record**:
The minimal reusable decision signature, selected action, and provenance of a
submitted Review Queue decision after unselected candidate content has been
deleted. A later matching issue may replay the decision without retaining the
rejected source text.
_Avoid_: rejected revision

**Reconciliation**:
Comparison of reimported content with current knowledge to classify it as a
duplicate, an addition, or a conflict.
_Avoid_: overwrite

**Conflict Queue**:
A persisted review list of conflicting knowledge updates that lets a user make
individual decisions or apply a version choice in bulk.
_Avoid_: exception module

**Graph-Augmented Retrieval**:
Use of the Knowledge Graph as an additional retrieval channel while ordinary
document retrieval remains the safe fallback for a Grounded Answer.
_Avoid_: graph-only search

**Capability Degradation**:
A visible, operation-specific condition in which an optional processing or
retrieval capability is unavailable while a safe fallback remains available.
Its primary message states the user impact and recovery action; technical codes
remain available in details and Application Logs.
_Avoid_: successful completion

**Quarantined Document**:
A source document whose import exhausted automatic recovery and whose partial
results are excluded from the available knowledge base.
_Avoid_: skipped document

**Recovery Override**:
A user-confirmed configuration snapshot selected for one manual recovery of a
Quarantined Document that does not modify the knowledge base's default
configuration. It may select model capabilities, reasoning, a Knowledge
Analysis Replan, or a Parser Route Override but cannot introduce a model
response deadline.
_Avoid_: knowledge-base setting
