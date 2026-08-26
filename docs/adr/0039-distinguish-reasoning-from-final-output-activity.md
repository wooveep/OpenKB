# Distinguish reasoning from final model output activity

Provider-labeled reasoning chunks produce Reasoning Output Activity while the
Model Call Status remains `running`; final-content chunks produce Model Output
Activity. Both states expose only sanitized activity metadata and never display
or persist raw chunks. This refines ADR-0037 so the task surface can report real
transport activity without calling reasoning a valid result or claiming
semantic progress.

Reasoning Output Activity never controls a timeout or retry. If the provider
terminates after reasoning without final content, the attempt ends as a Model
Result Failure rather than being treated as completed or as invalid structured
output. ADR-0038's Reasoning Output Exhaustion is the specific case whose finish
reason reports the output limit.

A successful provider request that yields no usable final result ends with the
distinct Model Result Failure lifecycle status and a specific non-retryable
failure code; it is not a Provider Failure. Model Usage Records retain only
finish reason, provider token usage, and content-free booleans and counts for
reasoning and final-output chunks and characters. Raw reasoning and final
content remain excluded from usage records, Application Logs, and Diagnostic
Bundles.

A completely empty final response with no reasoning ends as
`empty_final_result` under Model Result Failure. Structured Output Repair is
reserved for nonempty structured content that fails local validation; an empty
response provides no result to repair and does not authorize another paid Model
Call.

These lifecycle, failure, and safe-diagnostic rules apply to every Model Call,
not only structured Analysis. Reasoning with no final content ends as
`reasoning_output_exhausted` when an output-limit finish reason is observed and
as `reasoning_only_result` otherwise. A reasoning-only Answer becomes an
Interrupted Answer and offers an explicit Answer Retry; raw reasoning is never
promoted to answer text and OpenKB never retries it automatically.

The Task Drawer presents a concise failure explanation and keeps finish reason,
reasoning/final-output presence, chunk and character counts, and provider token
usage in expandable technical details. Diagnostic Bundles include the same
content-free fields. Historical records without those observations remain
unchanged with nullable new fields; OpenKB does not infer a new lifecycle status
from an old `model_response_invalid` code.
