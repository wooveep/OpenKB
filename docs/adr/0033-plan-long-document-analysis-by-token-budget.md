# Plan long-document analysis by token budget

Knowledge Analysis creates an immutable Knowledge Analysis Plan before its
first Model Call. The plan pins the DocumentIR digest, Analysis Model, Prompt
Contract, conservative input and output token budgets, natural-section batch
boundaries, and merge topology. A provider's known context capacity may inform
the budget; an unknown model uses a conservative code-owned fallback. Recovery
continues the pinned plan, while changed model settings or prompt contracts
apply only to a new import or explicit Knowledge Reanalysis.

Knowledge Analysis Batches follow natural DocumentIR or PageTree sections while
fitting the token budget instead of using a fixed Evidence count. This refines
ADR-0029's resumable long-document analysis and avoids both dozens of tiny calls
and a whole-document request that exceeds model context. Completed batch
outputs remain validated, independently checkpointed inputs to the merge.

A knowledge base schedules at most two Analysis Model Attempts concurrently by
default, with an advanced setting from one through four. Scheduling is fair
across documents and follows the Model Retry Policy, including provider
`Retry-After` instructions. A permanent failure or exhausted retry policy stops
new dispatch; already active attempts may finish and checkpoint. The document
remains unavailable and enters manual recovery with all completed work intact.

Knowledge Analysis Merge first combines exact entities, aliases, tags, claims,
and evidence links deterministically. It then uses checkpointed, token-bounded
hierarchical Model Calls only for document summaries and unresolved semantic
conflicts. A single prompt containing every batch result is not a supported
merge strategy.

If usable DocumentIR is complete but the Analysis Model is missing or its
capability check fails, the Import Job becomes Awaiting Model Configuration
rather than discarding parser work or publishing without Knowledge Analysis.
The user explicitly resumes it after correcting settings.

As a planner acceptance fixture, a representative 662-block DOCX that currently
requires about 71 fixed-size batches must require no more than 20 analysis
batches under the conservative 16K Model Capability Profile, with no Prompt
Contract budget violation.
