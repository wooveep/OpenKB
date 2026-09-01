---
status: accepted
---

# Render PageTree Selection contracts for JSON-object models

DeepSeek `json_object` mode guarantees JSON syntax but does not enforce the
PageTree Selection schema attached to OpenKB's internal request. PageTree
Selection and its one permitted Structured Output Repair therefore render the
complete code-owned schema, example, validation rules, and contract version in
the provider-visible system message.

The schema and provider-visible instructions declare the existing bounds of
three documents and twelve node IDs per document. Local validation reports
which content-free boundary failed so Structured Output Repair can narrow an
over-broad selection instead of repeating it. The PageTree Selection Prompt
Contract receives a version bump so an existing operation suspension cannot be
mistaken for the revised behavior.

The shared Analysis capability remains valid because the transport adapter,
model, and capability probe are unchanged; only this operation's exact contract
state is new. OpenKB does not silently truncate or invent a model choice. Local
validation, the one-repair policy, and deterministic retrieval fallback remain
unchanged.
