# Reserve model capacity for interactive answers

Background Knowledge Analysis, PageTree Enrichment, graph extraction, and
reanalysis use the knowledge base's Analysis Concurrency, which defaults to two
and is adjustable from one through four. At least one high-priority Interactive
Model Lane is reserved for the `retrieval_plan` and `grounded_answer` pipeline.
An Analysis Model Attempt that waits indefinitely therefore cannot consume all
capacity needed to answer from already Available Knowledge.

The reserved lane is scheduling priority, not a promise that an external
provider will avoid rate limiting. Explicit 429 or other provider responses
still follow the Model Retry Policy, and the UI reports whether an interactive
request is queued, awaiting the model, streaming, or interrupted.
