# Desktop retrieval ablation gate

The local Knowledge Graph is a conservative, evidence-only retrieval channel.
New Desktop knowledge bases keep it out of default question answering until a
fixed corpus-snapshot suite demonstrates an incremental gain over the baseline.
Community detection, Global GraphRAG, DRIFT, embeddings, and vector indexes are
not evaluation variants and cannot be enabled by this gate.

Run the report from the repository with a populated Desktop knowledge base:

```bash
uv run python -m openkb.desktop_retrieval_evaluation <kb-dir> <suite.json> \
  --repetitions 3 --output <report.json> --promote-local-graph
```

The command exits with code `2` when the gate does not pass, while still writing
the complete report. Its JSON suite is versioned and corpus-snapshot-bound:

```json
{
  "schema_version": 1,
  "snapshot_id": "support-corpus-2026-08-15",
  "max_graph_latency_ms": 250,
  "max_graph_model_calls": 20,
  "cases": [
    {
      "case_id": "release-path",
      "category": "multi_hop",
      "question": "Which components form the release path?",
      "expected_evidence": [
        {"document_name": "architecture.md", "text_contains": "Release coordinator"}
      ],
      "expected_answer_terms": ["Release coordinator"]
    }
  ]
}
```

Include at least one case in each category: `local_fact`, `multi_hop`,
`cross_document_conflict`, `global_theme`, and `absent_answer`. An
`absent_answer` case has an empty `expected_evidence` array and
`"expect_absent_answer": true`. Selectors are resolved against available
document names and original evidence text so the suite remains valid when an
import assigns new random Evidence IDs.

Each report records FTS, PageTree, Wiki, fused baseline, and local-graph
Recall@K (including the active K), citation precision, grounded-answer
faithfulness, mean latency, and model invocation counts/input-output character
costs. When a model is configured, the command invokes the same
`grounded_answer` prompt and Model Gateway policy as Desktop question answering;
without one it records the normal deterministic fallback. Character counts are
reported instead of fabricating a provider price.

The gate requires a strict local-graph Recall@K improvement, no per-case loss
of expected evidence, no citation or faithfulness regression, and any suite
latency/call ceilings. `--promote-local-graph` is explicit and takes effect
only after a passing gate. It persists the suite digest and enables the local
graph for ordinary Desktop answers in that knowledge base. Python callers may
instead invoke `DesktopRetrievalEvaluator.promote_local_graph(report)`.

Reports also include an opaque `knowledge_snapshot_digest` calculated from the
available documents, evidence, published knowledge pages, and local graph.
Promotion verifies that digest inside the knowledge-base lock, so a report
cannot enable another knowledge base or a corpus that changed while the
evaluation ran. The report and approval also carry a lightweight retrieval
revision maintained by SQLite on every retrieval-affecting write. Ordinary
answers compare only that revision, so after an import or published
knowledge-page change local graph retrieval remains off until the suite is run
again without adding a full-corpus scan to question latency.
