---
status: accepted
---

# Render Knowledge Analysis contracts for JSON-object models

DeepSeek's `json_object` mode guarantees JSON syntax but does not enforce the
response schema attached to OpenKB's internal request. Knowledge Analysis had
only rendered prose instructions and an empty-array example, so real document
batches could return unsupported top-level fields or non-canonical claim roles.
The single structured-output repair then received a schema that did not fully
match the local validator: `document_summary`, `procedures`, `role`, and
`applicability` were optional in JSON Schema but required for corpus-ready local
acceptance, and the shared candidate schema allowed `subtype` on non-Entities.

Knowledge Analysis, batch analysis, and description merge now adopt the shared
provider-visible Contract Renderer already used by graph extraction. Their
complete schema, example, validation rules, and version are visible whenever a
provider uses non-authoritative structured output such as DeepSeek
`json_object`; native provider selection remains unchanged. Repair requests also
retain the parent Knowledge Analysis contract.

The code-owned JSON Schema now matches the strict corpus-ready local shape:
document summaries and procedures are required, claim role and applicability
are required, and only Entity candidates may contain subtype. The three prompt
contracts receive explicit version bumps so persisted plans and retry permits
cannot mix the old and new provider-visible behavior.

This change does not weaken local validation, add another automatic repair, or
inspect raw model output. Regression tests pin both schema/validator alignment
and the actual DeepSeek system message at the transport boundary.

Description merge has one mechanical exception: an otherwise valid
`document_description` longer than the persisted 4,000-character bound is
deterministically shortened at the latest available sentence boundary. Exact
claims, sources, candidates, and document-summary units are merged separately
and are never truncated. A paid model repair is reserved for semantic or shape
errors that cannot be corrected losslessly at this boundary.

Batch-produced Document Summary units are exact-deduplicated and bounded to 32
at aggregate read and write boundaries. Selection always retains every present
summary role and samples the ordered units across the beginning, middle, and end
of the document. This keeps long manuals navigable without making an additional
summary call; omitted Guidance does not remove any candidate claim or Evidence.
