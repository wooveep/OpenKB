# Scope invalid model results to one operation contract

A locally invalid structured result suspends only its exact model operation,
Model Execution Profile, and Prompt Contract combination. The shared profile
loses verification only after a failed capability check or a confirmed shared
adapter-protocol breach; an uncertain breach requires the same signature from
two independent operations, and schema or domain-validation failures never
qualify. The unchanged suspended combination resumes only by explicit retry,
while a profile or contract change creates a new unverified state without an
automatic provider call.

For interactive answers, Regenerate is the explicit retry for Retrieval Plan
and PageTree Selection. For Analysis pipelines, a valid Recover Import or
Start/Retry Knowledge Reanalysis action opens an action-scoped retry round for
the affected contracts. Parallel batches may join that round until a terminal
validated or failed result closes the exact permit; cancellation and local
pre-dispatch failure revoke unused permits. New questions, imports, and later
actions continue to honor any suspension. Every actual provider request is
gated and recorded by its request identity and Prompt Contract digest, including
a Structured Output Repair contract derived from its parent operation, parent
contract, and response schema. A valid repaired result marks both parent and
repair contracts ready.
