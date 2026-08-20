# Separate document lineage from knowledge reconciliation

Document lineage is not inferred from matching entities or concepts. D0 and D1
reuse exact content, D2 reuses evidence without merging document identity, and
D3 creates a Document Version Candidate that joins an existing version chain
only after user confirmation; otherwise it becomes a new document. Concept and
entity changes are handled separately by Knowledge Reconciliation, which may
merge compatible additions while sending Conflicts to the Review Queue.
