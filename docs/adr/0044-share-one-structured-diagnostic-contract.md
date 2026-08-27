# Share one structured diagnostic contract across the Desktop Runtime

Desktop Shell and Python Engine write separate rotating JSON Lines Application
Logs under one versioned, support-safe Diagnostic Event contract and correlate
them with one Runtime Session ID. Desktop Shell is the sole parser of Portable
Local Configuration and passes the normalized level to the Engine; the levels
are `TRACE`, `DEBUG`, `INFO`, `WARN`, and `ERROR`, with `WARN` as the default.
Higher verbosity adds bounded lifecycle, decision, timing, count, and sanitized
cause metadata but never source content, prompts, model payloads, raw reasoning,
credentials, raw paths, or unbounded loop detail. This supplements ADR-0018's
diagnostic trust boundary and trades the convenience of independent plaintext
loggers for stable cross-process Failure Attribution and machine-verifiable
privacy and noise limits.

ADR-0045 later supersedes only the TRACE payload-exclusion clause by moving
explicitly authorized raw failure evidence into a separate Sensitive Trace
Capture. Application Logs and Diagnostic Bundles retain this ADR's support-safe
contract at every level.

One Failure Owner emits the canonical terminal event; propagation boundaries
reference its identity instead of repeating it. A global level may be narrowed
or expanded for the fixed Diagnostic Components, and support-safe Shell and
Engine log tails are included in a user-reviewed Diagnostic Bundle while every
Sensitive Trace Capture remains excluded.

At the default `WARN` level, each terminal event carries a self-contained
Failure Context with stage-specific safe observations, so diagnosis never
depends on lower-verbosity history. `TRACE`, `DEBUG`, and `INFO` use a bounded
queue while `WARN` and `ERROR` synchronously flush; logging failure or queue
pressure never fails a domain operation, and dropped-event counts remain
observable. Diagnostic Bundles include at most the latest 10 MiB from each
support-safe process log.
