# Default structured analysis reasoning to off through provider-aware capabilities

Structured Analysis Model operations resolve reasoning from an explicit
Analysis value, then an explicit default-role value, and finally `off`;
`low`, `medium`, or `high` opts in.
The provider-aware Model Capability Profile owns whether and how that control is
encoded instead of relying on model-name-only guesses or exposing arbitrary raw
provider parameters. This partially supersedes ADR-0030's provider-default
reasoning behavior for the Analysis Model: reliable final structured output
within a pinned budget outweighs implicit reasoning, while the Answer Model
continues to use provider behavior when its reasoning setting is absent.

The first Model Provider Adapter implements DeepSeek's explicit reasoning
controls and stream semantics. A Custom Model Provider cannot serve as an
Analysis Model because OpenKB cannot encode a trustworthy `off` or validate its
protocol without a named, code-owned adapter; it may serve as an Answer Model
after a successful streaming capability check. OpenKB neither guesses from
custom model names nor accepts arbitrary raw provider parameters.

An absent Analysis reasoning value inherits an explicit default-role reasoning
value; when both are absent, including `null` in an existing knowledge base, it
resolves to `off` without rewriting configuration. A Knowledge Analysis Plan
pins the resulting Model Execution Profile rather than consulting later
settings during recovery. When reasoning is explicitly enabled, the plan
reserves final structured-output capacity separately from a bounded allowance
for the selected reasoning level and sends their combined ceiling to the
provider.

The Reasoning Token Allowance is `0.5×`, `1×`, or `2×` the final structured-
output reserve for `low`, `medium`, or `high`. Planning reduces document input
per batch to make room but never silently lowers the selected reasoning level;
the Model Capability Check fails with an actionable capacity result when even a
minimum useful batch cannot fit.

If a provider reaches its output limit after emitting reasoning but before
returning final content, OpenKB records Reasoning Output Exhaustion as an
explicit non-retryable failure. It neither parses raw reasoning as the result
nor silently retries with changed settings, preserving Knowledge Analysis Plan
semantics and avoiding unapproved duplicate model cost.

Model Configuration displays Effective Model Role Settings beside the selected
values. An inherited Analysis value therefore reads, for example, “inherit
Default (effective: off)” instead of the ambiguous “provider default,” and
reasoning choices unsupported by the selected Model Provider Adapter are
disabled with an explanation.
