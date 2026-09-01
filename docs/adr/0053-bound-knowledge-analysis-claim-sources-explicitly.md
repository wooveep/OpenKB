---
status: accepted
---

# Bound Knowledge Analysis claim sources explicitly

Knowledge Analysis accepts at most 16 unique Evidence IDs per claim. One shared
constant owns the local validator, JSON Schema, prompt instructions, and repair
error, so the provider-visible contract cannot drift from the acceptance
boundary. Sixteen preserves legitimate multi-section claims observed with ten
known batch sources while keeping generated source maps bounded; unknown or
cross-batch Evidence IDs remain invalid regardless of cardinality.

`source_evidence_ids` belongs only inside claims. Candidate-level copies remain
invalid, and the repair error states that field placement without echoing
untrusted field values.
