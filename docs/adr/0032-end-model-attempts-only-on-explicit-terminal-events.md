# End model attempts only on explicit terminal events

OpenKB imposes no application-level total, response/read, thinking, or
generation deadline on an established Model Attempt. The attempt remains
Awaiting Model Result until the provider returns a valid response or explicit
error, the transport reports a connection failure or disconnect, the user
cancels, or the application shuts down. Provider-side reasoning, queuing, and
output generation are therefore included in an open-ended wait, and elapsed
time alone never becomes a failure.

This supersedes the model deadline and response-timeout portions of ADR-0001,
ADR-0002, and ADR-0025. Their quarantine, recoverability, and optional
degradation boundaries remain in force where later ADRs have not changed them.

A 30-second DNS/TCP/TLS connection-establishment bound remains a Network
Failure. Once the request is sent, OpenKB sets no first-byte, read, or total
response timeout. An HTTP 408 or 504 or another explicit API error is a Provider
Failure and may retain the provider's timeout detail, but OpenKB does not
relabel it as its own Model Timeout. This replaces the fixed response deadline
and deadline-budgeted retry behavior.

One Model Call may make at most three Model Attempts. Explicitly transient
Provider Failures such as 408, 429, and 5xx responses and Network Failures use
`Retry-After` when supplied or bounded backoff otherwise; authentication,
input, permission, and other permanent failures stop immediately. There is no
enclosing elapsed-time budget, so any individual attempt may wait indefinitely.

Because a silent provider may occupy a worker indefinitely, the UI exposes
"Awaiting model result," elapsed wait, Knowledge Analysis Batch progress,
attempt count, and a cancellation action. It must not claim that the model is
thinking or making progress when the non-streaming provider interface exposes
no such evidence. Cancellation is best effort because some providers may keep
computing and charging after their client disconnects.

User cancellation and application shutdown produce an Interrupted Import Job,
not a failed or Quarantined Document. Completed Stage Runs and Knowledge
Analysis Batches remain available for recovery, and restarting the application
does not silently resume model usage; the user explicitly resumes the job.

Model Configuration no longer exposes or honors a response-timeout field.
Configuration migration ignores the legacy `initial_timeout_seconds` value;
only the fixed 30-second DNS/TCP/TLS connection-establishment bound remains.
Recovery Overrides may still select a model or capability values but cannot
restore a response deadline.

The acceptance suite includes a controlled provider that stays silent for 180
seconds and then succeeds: OpenKB must complete it with exactly one Model
Attempt and no failure or retry. Separate cases cover connect timeout,
disconnect, 408, 429, 5xx, authentication failure, cancellation, and application
shutdown with their agreed classifications and checkpoint behavior.
