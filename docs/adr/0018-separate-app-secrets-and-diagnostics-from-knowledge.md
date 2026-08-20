# Separate application state, secrets, and diagnostics from knowledge data

The program directory remains read-only, user-level application state and
sanitized logs live under the Windows local application-data area, and durable
model credentials use Windows Credential Manager or DPAPI while knowledge-base
configuration stores references only. OpenKB sends no automatic telemetry and
exports a Diagnostic Bundle only after user review, excluding source content,
model payloads, and credentials; this keeps portable upgrades, knowledge
backup, and support data as distinct trust boundaries.
