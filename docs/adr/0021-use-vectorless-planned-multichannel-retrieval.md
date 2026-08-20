# Use vectorless planned multichannel retrieval

OpenKB does not use Embedding models, vector indexes, or a vector database.
Question answering may use a bounded LLM-generated Retrieval Plan and one
bounded candidate rerank, but candidate generation remains the combination of
SQLite FTS5 (`unicode61` plus `trigram`), corpus and document PageTrees,
materialized Knowledge Pages, entity aliases, and an evidence-backed SQLite
Knowledge Graph. Channels normalize to EvidenceRef candidates and use RRF with
a protected baseline quota; planning, reranking, graph extraction, or graph
lookup failure falls back to deterministic baseline retrieval. The graph is
adapted from the extraction and local-expansion ideas in Neo4j Labs LLM Graph
Builder, not from its Neo4j, Cypher, APOC, GDS, LangChain, or vector runtime.
OKF knowledge and PageTree selections are routing and ranking signals only:
they also normalize to EvidenceRef candidates, cannot replace the protected
FTS and Structure Lexical baseline, and never enter Answer Evidence as
generated prose.
