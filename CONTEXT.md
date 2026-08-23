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
An automatically maintained, rotating diagnostic record kept in application
state rather than in a Desktop Knowledge Base or the program directory.
_Avoid_: console output, Diagnostic Bundle

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
but mandatory Knowledge Analysis cannot start because its Analysis Model is not
configured or available. The document remains unpublished until explicit
resume succeeds.
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
The stable identity of one projected Knowledge Page, derived from its immutable
page or generation identity rather than its editable title.
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
The context capacity, native structured-output support, and optional reasoning
setting resolved for a configured model from known metadata and advanced user
overrides. Unknown models use a 16K-token context assumption; reasoning defaults
to provider behavior and unsupported reasoning settings are omitted.
_Avoid_: Prompt Contract, provider promise

**Model Capability Check**:
A cancellable, user-initiated check of each distinct configured model. Analysis
Models must demonstrate schema-valid structured output and Answer Models must
demonstrate streaming; generated check content is not persisted.
_Avoid_: TCP probe, document analysis, health telemetry

**Interactive Model Lane**:
At least one high-priority model execution slot reserved for an interactive
`retrieval_plan` and `grounded_answer` pipeline so background imports cannot
starve questions over already Available Knowledge.
_Avoid_: background Analysis Concurrency, unlimited answer concurrency

**Prompt Contract**:
The code-owned, versioned combination of model instructions, input shape,
output schema, validation rules, and bounded generation policy for one operation.
Knowledge Analysis Plans retain its canonical snapshot and digest for recovery.
_Avoid_: AGENTS.md prompt, user prompt override

**Structured Output Repair**:
The single Analysis Model call allowed after deterministic normalization and
local schema validation cannot make a structured result valid. It receives the
validation errors and evidence-bound source material; a second invalid result
ends automatic recovery.
_Avoid_: transport retry, unbounded self-correction

**Model Call**:
A logical request for one OpenKB result from a configured model, potentially
fulfilled by several Model Attempts. It has no OpenKB-imposed total, read,
thinking, or generation deadline; elapsed time alone never ends it.
_Avoid_: API request

**Model Attempt**:
One provider request issued in pursuit of a Model Call. It ends only with a
valid provider response, an explicit Provider Failure, a Network Failure, user
cancellation, or application shutdown. Cancellation is best effort and does
not guarantee that a provider without cancellation support stops computing.
_Avoid_: retry

**Model Retry Policy**:
At most three Model Attempts for one Model Call, with `Retry-After` or bounded
backoff between attempts, and no enclosing time budget. Only explicit transient
Provider Failures and Network Failures are retryable; authentication, input,
permission, and other permanent failures stop immediately.
_Avoid_: deadline retry, retry forever

**Awaiting Model Result**:
The nonterminal Model Attempt state after request dispatch and before a terminal
provider, transport, cancellation, or shutdown event. It means OpenKB is still
waiting, not that it can observe provider-side reasoning or token generation.
The UI reports this state with elapsed wait, batch progress, attempt count, and
a cancellation action.
_Avoid_: model thinking, hung model, model timeout

**Model Output Activity**:
An observed response chunk from a streaming Model Attempt. Structured output is
buffered for final validation while the UI may report that output is arriving;
raw chunks are not displayed or persisted, and activity never controls timeout.
_Avoid_: reasoning progress, validated result, heartbeat deadline

**Long Wait Advisory**:
A nonterminal notice shown after a Model Attempt has waited longer than the
greater of five minutes or twice that role/model's local historical P95. It
offers cancellation but never fails or retries the attempt.
_Avoid_: warning timeout, model deadline

**Model Usage Record**:
The local per-call record of role, model, IDs, batch, queue/connect/first-output
and total timing, classified result, call count, and provider-reported tokens.
Missing token counts are visibly estimated, and currency is shown only when the
user configures pricing.
_Avoid_: source telemetry, inferred provider bill

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
document as source-backed Concepts, Entities, aliases, tags, and claims before
that Document Version may be published.
_Avoid_: provider connection test, graph extraction, PageTree Enrichment

**Knowledge Analysis Batch**:
A recoverable, token-budgeted analysis checkpoint for one group of natural
DocumentIR sections. Its validated result is reused when the containing
Knowledge Analysis resumes.
_Avoid_: Import Batch, whole-document retry

**Knowledge Analysis Plan**:
The immutable execution manifest created when a Knowledge Analysis starts. It
pins the DocumentIR digest, Analysis Model, Prompt Contract, input and output
budgets, natural-section batch boundaries, and merge topology so recovery does
not mix incompatible results. For a model with unknown capacity, it assumes a
16K-token context and allows approximately 8K tokens of document input per
batch, reserving the remainder for instructions and output.
_Avoid_: latest model settings, mutable batch queue

**Knowledge Analysis Merge**:
The checkpointed document-level reduction of completed Knowledge Analysis
Batches. It deterministically combines identical entities, aliases, tags,
claims, and evidence links, then uses token-bounded hierarchical Model Calls
only for document summaries and unresolved conflicts.
_Avoid_: one giant merge prompt, concatenation

**Outdated Knowledge Analysis**:
A still-usable Knowledge Analysis produced by an older analysis schema, prompt,
or engine version and eligible for user-initiated Knowledge Reanalysis.
_Avoid_: failed analysis, automatic migration

**Knowledge Reanalysis**:
An explicit regeneration of Knowledge Analysis for an already imported
Document Version using current model behavior without reimporting its Raw Asset.
_Avoid_: duplicate import, automatic D1 analysis

**Unmapped Knowledge Revision**:
An existing Knowledge Page revision that predates the Knowledge Source Map and
may support browsing and routing but not claim-level evidence selection.
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
state after its Model Call fails, until a successful Answer Retry replaces it.
_Avoid_: completed answer

**Answer Retry**:
A renewed Model Call for an Interrupted Answer that replaces that answer in
place only once a completed response is available.
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
Completed, non-quarantined document results currently eligible for Grounded
Answers, even while other Import Jobs remain active.
_Avoid_: complete batch

**Retrieval Plan**:
A validated, bounded structure derived from a question that records intent,
lexical expansions, named entities, concepts, time constraints, and requested
answer scope for the retrieval channels. Failure to create one does not disable
deterministic baseline retrieval.
_Avoid_: generated search query

**Vectorless Semantic Retrieval**:
Semantic candidate discovery without Embedding models or vector indexes. It
combines an optional Retrieval Plan with PageTree reasoning, FTS5, Knowledge
Pages, entity aliases, and evidence-backed graph traversal.
_Avoid_: vector search

**Evidence Pack**:
The bounded, generation-consistent set of ranked EvidenceRefs, source
coordinates, Source Images, channel reasons, and degradation state given to an
answer model. It contains no unpublished graph candidates or Quarantined
Document results.
_Avoid_: prompt context

**Retrieval Trace**:
The compact record of Catalog and Document PageTree generations, selected
channels, candidate reasons, and degradation state that explains how an Answer
Version obtained its immutable Answer Evidence.
_Avoid_: model chain of thought, regenerated search

**Entity Resolution Candidate**:
A possible identity match generated from normalized names, aliases, types,
lexical similarity, and optional bounded LLM judgment. A D3 candidate requires
review and never merges entities automatically.
_Avoid_: resolved entity

**Knowledge Graph**:
An internal, evidence-backed relationship structure used to improve retrieval
and Grounded Answers; it is not a first-release Desktop Workbench browsing
surface.
_Avoid_: graph view

**Knowledge Page**:
A user-editable representation of an approved concept or entity in a knowledge
base.
_Avoid_: source document

**Current Published Revision**:
The one Knowledge Page revision currently eligible for navigation and, through
its source bindings, retrieval; it remains active while a newer draft is edited.
_Avoid_: latest saved draft, generation candidate

**Working Draft**:
The recoverable, unpublished revision being edited for one Knowledge Page,
which may coexist with its Current Published Revision.
_Avoid_: Current Published Revision, temporary editor text

**Knowledge Publication**:
The explicit user action that atomically promotes a Working Draft which passes
the Publication Gate to become the Current Published Revision.
_Avoid_: autosave, verification

**Knowledge Verification**:
An explicit human confirmation of one complete Knowledge Page revision and its
sources. Any subsequent content, source, or lifecycle change requires a new
verification.
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
Knowledge Page revision, none of which determines the page's stable identity.
_Avoid_: Concept ID, generated taxonomy

**Draft Revision**:
A saved Knowledge Page revision that is not eligible for Grounded Answers
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
A statement in a Knowledge Page whose OKF source marker resolves to an
EvidenceRef in Available Knowledge. It may route and rank source evidence but
is never itself Answer Evidence.
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
markers.
_Avoid_: manual Evidence ID entry, page-level source

**Missing Source Candidate**:
A model-generated knowledge claim that cannot resolve to supporting evidence
and remains outside published knowledge until a user binds a source or discards
it.
_Avoid_: Quarantined Document, stable knowledge

**User Revision**:
A user-authored revision of a Knowledge Page stored in authority data and
materialized to Markdown.
_Avoid_: Markdown edit

**Conflict**:
An incompatible incoming change to approved knowledge that cannot be merged
automatically with an existing revision.
_Avoid_: import failure

**Knowledge Reconciliation**:
Classification of incoming concept and entity changes as compatible additions
or Conflicts against approved knowledge.
_Avoid_: document deduplication

**Three-way Knowledge Reconciliation**:
Review of Current Published Revision, Working Draft, and incoming knowledge
when an import affects a page with unpublished user work.
_Avoid_: automatic draft merge, document version review

**Knowledge Deprecation**:
The reversible lifecycle change that removes a published Knowledge Page from
default routing while retaining its identity, content, and history.
_Avoid_: permanent deletion

**Review Queue**:
A persisted collection of Conflicts and other unresolved knowledge candidates
awaiting a user's individual or batch resolution.
_Avoid_: exception module

**Resolution Record**:
The minimal retained record of a submitted Conflict decision after unselected
candidate content has been deleted.
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
A visible condition in which an optional processing or retrieval capability is
unavailable while a safe, usable fallback remains available.
_Avoid_: successful completion

**Quarantined Document**:
A source document whose import exhausted automatic recovery and whose partial
results are excluded from the available knowledge base.
_Avoid_: skipped document

**Recovery Override**:
A configuration snapshot selected for one manual recovery of a Quarantined
Document that does not modify the knowledge base's default configuration. It
may select model capabilities or a Parser Route Override but cannot introduce a
model response deadline.
_Avoid_: knowledge-base setting
