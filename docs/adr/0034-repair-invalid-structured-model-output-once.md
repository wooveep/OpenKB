# Repair invalid structured model output once

Structured Prompt Contracts prefer a provider's native JSON Schema response
format. When native schema is unavailable, they request JSON through ordinary
messages and apply the same local schema validation. Deterministic normalization
may remove transport-style wrappers such as Markdown fences, but it must not
invent fields, evidence links, or semantic content.

If normalization still leaves an invalid result, OpenKB permits one Structured
Output Repair using the Analysis Model. The repair receives the validation
errors, invalid result, applicable schema, and original evidence-bound source
material so it cannot silently detach claims from their provenance. The repair
has its own Model Call and therefore follows the ordinary Model Retry Policy for
explicit transient provider or network failures.

A second invalid result ends automatic recovery and leaves the document
unavailable for manual recovery. This limited correction supersedes ADR-0029's
and ADR-0001's immediate non-retryable handling of the first unparseable
response; it does not permit an unbounded semantic repair loop or skip
validation.
