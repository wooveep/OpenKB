# Semantic Quality Evaluation

This repository-only gate evaluates the two production operations that own dynamic semantic
structure: `query_planning` and `knowledge_page_planning`. It uses the same Prompt Contracts,
provider adapter, one-repair boundary, and local validators as the Python Engine. It is not a
runtime activation gate and does not use a second model as a judge.

## Live run

Put the evaluation credential only in the repository-local, Git-ignored `.env`:

```dotenv
LLM_API_KEY=your-local-evaluation-key
```

Then run:

```bash
uv run python -m evaluation.semantic_quality run --candidate-release
```

The pinned profile uses `https://api.deepseek.com`, `deepseek-v4-flash`, JSON Object output,
disabled thinking, and three repetitions per matrix case. A normal development run without a key
prints `SKIPPED` and exits successfully. Candidate-release mode without a key exits with an
actionable error.

Each run is written under `.semantic-eval/<run-id>/`. `outputs.jsonl` contains the complete model
results needed for human review and is intentionally ignored by Git. `report.json` contains only
content-free counts and digests. `attestation.pending.json` cannot claim that semantic quality
passed; a deterministic success is always `pending_human_review`.

Never commit `.env`, `outputs.jsonl`, copied prompts, model responses, or source excerpts. Never
paste the API key into a review or attestation.

## Human review and sign-off

Review every repetition in `outputs.jsonl` against `matrix.json` and every dimension in
`rubric.json`. Each suite and the declared English/Chinese pair must pass independently; averages
cannot mask a failure. Write the verdict manually in a separate JSON file:

```json
{
  "schema_version": "openkb.semantic-quality-human-review.v1",
  "run_id": "<run-id>",
  "suites": [
    {
      "suite_id": "technical_operations_en",
      "dimensions": {
        "question_facets": "pass",
        "importance_judgment": "pass",
        "page_organization": "pass",
        "domain_and_language_fit": "pass",
        "evidence_respect": "pass"
      }
    }
  ],
  "pairs": [
    {
      "pair_id": "earth_seasons_translation",
      "dimensions": {
        "structural_equivalence": "pass",
        "language_naturalness": "pass"
      }
    }
  ]
}
```

The real file must contain exactly one entry for every suite and pair listed in the run report.
Each verdict is either `pass` or `fail`; optional `notes` may remain in the review file. Sign it
with an explicit maintainer identity:

```bash
uv run python -m evaluation.semantic_quality sign \
  .semantic-eval/<run-id> path/to/human-review.json \
  --maintainer "name-or-release-identity" \
  --output evaluation/semantic_quality/attestations/<run-id>.json
```

Signing verifies the raw-output digest and the pending report, then records only digests, the
non-secret pinned profile, deterministic counts, per-dimension human verdicts, maintainer identity,
and decision time. A run with any deterministic failure cannot be signed. A human `fail` produces
a signed failed attestation, never a passing aggregate.
