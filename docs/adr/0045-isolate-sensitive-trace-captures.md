# Isolate explicitly authorized sensitive trace captures

When Portable Local Configuration selects `TRACE`, sets
`allow_sensitive_trace` to true, and provides an expiry no more than 24 hours
away, OpenKB may retain unredacted evidence for failed operations in a separate
Sensitive Trace Capture. A failed model call may contribute its Prompt, request
body, assembled response, final output, and raw reasoning; parser failures may
contribute raw paths, process output, and exception stacks. Successful calls do
not contribute raw payloads, and OpenKB does not deliberately copy document
binaries, authorization headers, cookies, API keys, or SQL parameters, although
selected raw evidence may itself contain sensitive values. Sensitive Trace
Captures remain outside Application Logs and Diagnostic Bundles, and missing,
expired, or overlong authorization falls back to `WARN`. This supersedes only
ADR-0044's TRACE payload-exclusion clause, preserving its structured,
support-safe contract for ordinary logs in exchange for a narrow, explicit
diagnostic path that can reveal exactly what a failing provider returned.

Each capture uses a current-user-only plaintext directory under local application
state with a structured event index and separate raw payload files. Authorization
expiry stops a live capture and returns the effective level to `WARN`; closed
captures remain for at most 24 hours, with at most two captures and 100 MiB total.
One payload is bounded to 2 MiB with head/tail truncation, full length and digest,
and content-addressed deduplication. Expired data is normally deleted without a
secure-erasure claim.

The Desktop Workbench keeps an active capture visibly marked with its identity,
expiry, size, and TRACE components and can stop it for the current runtime.
Inspection opens the dedicated directory only after a sensitivity warning;
there is no bundle, archive, upload, or automatic sharing path. A capture
manifest records version, lifecycle, effective levels, completeness and
truncation counts, and a size-and-digest inventory without a username or machine
identifier. Component TRACE overrides remain subject to the same explicit
authorization, and expiry immediately returns every effective level to `WARN`.
