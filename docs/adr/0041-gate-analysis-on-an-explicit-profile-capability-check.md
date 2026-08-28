# Gate analysis on an explicit profile capability check

Saving Model Configuration never invokes a provider. A user explicitly runs a
cancellable Model Capability Check whose successful result is cached against
the complete Model Execution Profile; changing any pinned provider, model,
capability, reasoning, control-version, or budget value requires another check.
An Import Job whose profile lacks a successful check becomes Awaiting Model
Configuration instead of beginning paid structured analysis. This preserves
explicit model usage while preventing an unverified profile from failing across
an entire document.

The check is a small, bounded protocol-conformance request that exercises the
profile's actual streaming mode, reasoning control, finish reason, final
content, and schema validation; it is not a near-context-limit benchmark. After
upgrade, existing profiles have no trusted cache entry: published knowledge
remains available, while new or resumed Knowledge Analysis waits with an
explicit “run capability check” action. OpenKB does not grandfather the profile
or call the provider automatically.

A successful check has no time-based expiry. It is invalidated when any field
of the Model Execution Profile or its Model Provider Adapter version changes,
or when a later call produces a protocol-shaped Model Result Failure such as an
empty final result or incompatible response shape. Invalidation stops dispatch
of new analysis batches; already active Model Attempts may reach their explicit
terminal events and checkpoint only under their original pinned plan. It makes
the profile unverified rather than permanently unsupported; a later explicit
successful check can verify the same profile again.

ADR-0046 supersedes blanket profile invalidation for operation-specific invalid
results. ADR-0048 supersedes the save-only workflow while retaining explicit
user consent before any provider call.
