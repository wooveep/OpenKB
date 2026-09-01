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
  "max_navigator_model_calls_per_case": 8,
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

Each report records FTS, PageTree, Wiki, fused baseline, local-graph, and Navigator
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

After the required discovery channels have passed their own gates and are enabled,
run the same frozen suite as the Navigator release gate:

```bash
uv run python -m openkb.desktop_retrieval_evaluation <kb-dir> <suite.json> \
  --repetitions 3 --output <navigator-report.json> \
  --validate-navigator-promotion
```

This mode exits with code `2` unless Navigator recalls every reviewed critical
Evidence expectation, preserves baseline recall/citation/faithfulness and
absent-answer behavior, stays within the configured model/latency envelope,
finishes without degradation, and keeps the pinned knowledge snapshot stable.
Python callers may enforce the same stale-report check with
`DesktopRetrievalEvaluator.require_navigator_promotion_eligible(report, suite)`.

## Experimental official PageIndex provider

PageIndex remains absent from the default Engine environment and is disabled
for ordinary Desktop retrieval. Its 0.2.10 SDK requires a newer LiteLLM plus
Chat/Agents packages, so installing it into the Engine would replace audited
runtime pins. A Portable Desktop build carries a separate
`runtime/pageindex/OpenKBPageIndex.exe` onedir worker; source checkouts may
create the equivalent isolated Windows environment instead:

```powershell
pwsh -NoProfile -File desktop/scripts/New-PageIndexProviderEnvironment.ps1 `
  -Destination "$env:LOCALAPPDATA\OpenKB\pageindex-provider-0.2.10"
```

The lock uses the official MIT-licensed 0.2.10 wheel at verified release commit
`ba0ef02d78034704be049894c463dc606acbd0d7` and verifies wheel SHA-256
`23664dd05636d712eb597a7c9c326f4c14d0b3cf412cd3545662f833af641448`.
The normalized provider identity is
`official_pageindex@0.2.10+ba0ef02d7803.openkb1`, so an adapter contract change
cannot silently reuse an older generation.
Only `page_index_md` and its minimal deterministic runtime are installed; the
exact runtime set is PyPDF2 3.0.1, python-dotenv 1.2.2, and PyYAML 6.0.3.
The adapter never invokes PageIndex Chat, Agents, embeddings, or a vector store.

Run the same fixed suite against the experimental provider explicitly:

```powershell
uv run python -m openkb.desktop_retrieval_evaluation <kb-dir> <suite.json> `
  --experimental-pageindex-python `
  "$env:LOCALAPPDATA\OpenKB\pageindex-provider-0.2.10\Scripts\python.exe" `
  --repetitions 3 --output <pageindex-report.json>
```

To evaluate the exact worker shipped in a candidate Portable Package, use the
worker flag instead of the Python flag:

```powershell
uv run python -m openkb.desktop_retrieval_evaluation <kb-dir> <suite.json> `
  --experimental-pageindex-worker `
  "<portable-package>\runtime\pageindex\OpenKBPageIndex.exe" `
  --rebuild-official-pageindex --repetitions 3 `
  --output <pageindex-package-report.json>
```

OpenKB renders a temporary Markdown view from its SQLite Document IR, asks the
pinned SDK only for hierarchy, then normalizes nodes back to immutable OpenKB
PageTree generations and existing Evidence IDs. The temporary input is removed
after each call. The provider cache contains no full source text and can be
deleted; `--rebuild-official-pageindex` reconstructs it from SQLite authority.
Provider timeout, invalid output, missing runtime, or corrupt cache adds a
provider-local degradation to the complete report while baseline variants
continue. The flag never changes ordinary Desktop retrieval defaults or the
Grounded Answer service.
