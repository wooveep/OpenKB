# Allow model selection by capability role

Model Configuration retains one knowledge-base-scoped provider connection, API
Key, and default model, while allowing optional Analysis Model and Answer Model
selections that fall back to the default. This supersedes ADR-0023's deferral of
per-capability model selection: structured analysis benefits from a fast
schema-following model, while Grounded Answers may deliberately use a slower
reasoning model, without multiplying credential or endpoint configuration.

The Analysis Model handles `knowledge_analysis`, `knowledge_analysis_batch`,
`knowledge_analysis_merge`, `page_tree_enrichment`,
`knowledge_graph_extraction`, and `retrieval_plan`. The Answer Model handles
only `grounded_answer`. Thus the structured retrieval planner continues using
the Analysis Model even though it runs while answering a question.

Each role resolves a Model Capability Profile from known model metadata and
advanced overrides. An unknown model is conservatively treated as having a 16K
context, with roughly 8K available to document input after reserving room for
instructions and structured output. Reasoning uses the provider default unless
the user chooses `off`, `low`, `medium`, or `high` for that role; unsupported
reasoning parameters are omitted rather than emulated in prompt text.

The settings workflow performs a cancellable Model Capability Check for each
distinct configured default, Analysis, and Answer Model. It verifies schema-
valid structured output for an Analysis Model and streaming for an Answer Model
rather than treating TCP connectivity as sufficient. Check responses are not
persisted, and the wait follows the same explicit-terminal-event semantics as
every other Model Call.
