# Use the Knowledge Graph as a nonvisual retrieval channel

The first Desktop Workbench does not expose Knowledge Graph browsing or graph
visualization. Import still builds the graph from Available Knowledge and
question answering may use it to improve retrieval, directing the first
release's effort toward answer quality rather than a separate visualization
surface. Graph candidates are extracted directly from Evidence Fragments into
Entity, Concept, Claim, TypedEdge, and EvidenceRef records rather than inferred
back from materialized Knowledge Pages. Graph-Augmented Retrieval is additive:
the first release only performs evidence-anchored 1–2 hop expansion, graph
candidates must resolve to source evidence, and graph extraction or lookup
failure silently falls back to ordinary document retrieval while retaining
diagnostic details internally. Community/global GraphRAG remains deferred until
a fixed evaluation set proves an incremental benefit.
