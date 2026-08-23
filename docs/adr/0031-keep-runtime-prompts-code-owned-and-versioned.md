# Keep runtime prompts code-owned and versioned

Every model-backed operation uses a code-owned Prompt Contract that versions
its instructions, input shape, output schema, validation rules, and bounded
generation policy. Runtime prompts are not sourced from `AGENTS.md` or editable
per knowledge base because free-form overrides would make checkpoints,
provenance, validation, and evidence constraints unreliable; prompt
experimentation belongs in explicit development and evaluation tooling.

A Knowledge Analysis Plan persists a canonical snapshot and digest of the exact
Prompt Contract, including templates, schema, model parameters, and token-budget
policy. Recovery uses that snapshot rather than silently substituting current
code. A narrowly scoped security migration may explicitly invalidate an unsafe
snapshot; ordinary prompt improvements only mark completed results as Outdated
Knowledge Analysis and apply to new imports or Knowledge Reanalysis.

Provider-returned raw reasoning or chain-of-thought is neither logged nor
persisted in checkpoints, knowledge, or diagnostic bundles. OpenKB retains only
the final validated result, response hash, usage, timing, provider/model
identity, contract digest, and classified failures needed for provenance and
support.

A Prompt Contract change must update its version and pass fixed Chinese and
English fixtures covering long documents, prompt injection, missing evidence
links, and malformed JSON. Schema-valid output, valid Evidence references, and
checkpoint recovery may not regress relative to the accepted contract.
