# Use DeepSeek JSON Output through the provider adapter

DeepSeek JSON Output applies only to structured Analysis operations, including
`retrieval_plan` even when it runs inside a question-answering pipeline. It does
not apply to `grounded_answer`: the Answer Model streams user-facing natural
language and its request must omit `response_format`.

The DeepSeek Model Provider Adapter selects the strongest structured response
mode it explicitly supports: `json_schema`, then `json_object`, then a Prompt
Contract without native response formatting. The initially supported DeepSeek
contract uses `json_object`; it sends exactly
`response_format={"type":"json_object"}` and never treats that transport option
as schema enforcement. Changing the selected mode or adding verified
`json_schema` support requires an adapter-version change and therefore a new
Model Capability Check.

Every structured Prompt Contract contains the literal instruction to return
JSON and a code-owned minimal JSON output example consistent with its output
schema. The example, instructions, schema, and generation policy are part of
the canonical Prompt Contract snapshot and token budget. Dynamic Structured
Output Repair receives the original operation's schema and a canonical example
derived from it. Local normalization and schema validation remain mandatory in
all three modes.

The request's `max_tokens` is the plan's final structured-output reserve plus
its bounded reasoning allowance, after accounting for instructions, schema,
example, and document input within the model context. Planning reduces batch
input when necessary and never relies on a small provider default that could
truncate JSON. A nonempty truncated result may consume the one permitted
Structured Output Repair; OpenKB does not silently enlarge the budget or change
the pinned mode.

DeepSeek adapter `deepseek.v2` pins the documented V4 protocol as of 2026-08-28:
the named V4 models have a 1,000,000-token context and a 384,000-token maximum
output. That maximum is a hard provider ceiling, not the requested size of every
operation. Structured Analysis retains its schema-derived final reserve (32K
for the largest current relation contract);
long-form Answer requests use a bounded final-text plus reasoning allowance.
This keeps the remaining context available for evidence and prevents a small
JSON repair from becoming an open-ended 384K generation.
The small Answer Capability Check remains bounded and is not a near-limit
benchmark. An absent Answer reasoning choice leaves DeepSeek's enabled/high
provider default intact; explicit enabled choices send
`extra_body.thinking.type=enabled` and map OpenKB `low` to provider `low`, and
OpenKB `medium`/`high` to provider `high`. Structured Analysis retains its
explicit `disabled` thinking control. Prices remain user-supplied because the
provider's cache and time-of-day rates cannot be represented by one bundled
input price.

DeepSeek documents that JSON Output can occasionally return an empty `content`.
OpenKB classifies the non-retryable Model Result Failure from safe stream
evidence: observing neither reasoning nor final content is
`empty_final_result`; reasoning with no final content is
`reasoning_only_result`, or `reasoning_output_exhausted`
when the finish reason reports the output limit. Each returns the profile's
capability cache to unverified and stops dispatching new analysis batches. This
is not a permanent rejection: an explicit successful Model Capability Check can
verify the unchanged profile again. OpenKB neither performs a hidden paid retry
nor falls back from `json_object` to prompt-only output after the call; an empty
capability-check result simply fails the check.

Deterministic acceptance tests exercise the Engine/Desktop Bridge seam with a
real temporary SQLite knowledge base and scripted provider streams. Adapter
tests additionally cover raw reasoning, final-content, finish-reason, empty,
and truncated chunks, and assert that `grounded_answer` omits structured
response formatting, with only minimal task-surface UI tests. CI never calls a
paid provider. Before release, the exact packaged Windows build is checked
against DeepSeek using a disposable knowledge-base copy, never the user's
original knowledge base. Status normalization, adapter behavior, budgets,
failure classification, recovery, persistence migration, and task UI ship as
one atomic compatibility change.
