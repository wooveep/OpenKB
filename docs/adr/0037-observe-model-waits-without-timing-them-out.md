# Observe model waits without timing them out

Structured analysis uses streaming when the provider supports it, buffers the
chunks in memory, and validates only the completed JSON result. The task surface
may transition from "Awaiting first model response" to "Receiving model output"
after actual Model Output Activity, but it neither displays nor persists raw
chunks. Providers without compatible streaming use the non-streaming path and
remain Awaiting Model Result until an explicit terminal event.

Neither stream activity nor silence participates in failure detection. After
the greater of five minutes or twice the local historical P95 for that role and
model, OpenKB shows a Long Wait Advisory with elapsed time and cancellation.
The advisory does not end, retry, or label the Model Attempt as hung.

Import Progress exposes preflight, Raw Asset handling, parser initialization,
DocumentIR validation, Evidence construction, Knowledge Analysis planning,
completed/total batches, merge, and publication. Only durable stage and batch
completion advances progress; elapsed-time percentages are not shown.

Local Model Usage Records retain operation and role, provider/model, call and
attempt IDs, batch identity, queue/connect/first-output/total timing, HTTP
status, classified errors, call count, and provider-reported tokens. Missing
token usage may be estimated only when labeled as such. Currency appears only
after the user supplies pricing, not from a bundled price table.

These records remain in local task storage and rotating Application Logs. A
Diagnostic Bundle leaves the device only after explicit user export and omits
credentials, source content, Prompt Contract snapshots, model payloads, and raw
reasoning. OpenKB sends no automatic performance telemetry.
