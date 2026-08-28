# Unify generated and user knowledge in one workspace

The Knowledge Workspace browses immutable Generated Knowledge Items alongside
revisioned User Knowledge Pages without merging their identities or authority.
Editing generated knowledge is an explicit adoption that creates a separate
User Knowledge Page Working Draft with a new stable ID and immutable
`(generation_id, item_key)` origin reference, so a new analysis generation
cannot overwrite user-owned knowledge. A possible existing-page match enters
reconciliation instead of overwriting it and records the origin against the
selected page without changing page content. Ambiguous matches require an
explicit existing-page or separate-draft decision. The default workspace shows
current generated knowledge and user pages together, with older generations
retained as history. The Desktop Bridge adds a Knowledge Workspace contract
while preserving the existing user-page-only meaning of knowledge-page operations.
