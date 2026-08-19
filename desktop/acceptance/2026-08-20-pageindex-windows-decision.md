# PageIndex Windows packaging decision — 2026-08-20

## Decision

**Do not promote the official PageIndex provider.** Keep the deterministic
Document PageTree as the default and keep PageIndex packaged only as an
experimental, explicitly evaluated provider. No first-release Settings control
enables it.

This is the release decision for issue #63. The fixed evaluation completed, but
the promotion gate did not pass. The Windows 10 clean-machine row is also not
available from the single supplied Windows 11 test host, so the supported-OS
matrix is incomplete and cannot be treated as a promotion pass.

## Auditable evidence

- [Portable acceptance metrics](./2026-08-20-pageindex-portable-acceptance.json)
- [Fixed Retrieval Evaluation report](./2026-08-20-pageindex-retrieval-report.json)
- [Baseline release summary](./2026-08-20-pageindex-baseline-release.json)
- [Candidate release summary](./2026-08-20-pageindex-candidate-release.json)
- Fixed suite: [`../test-assets/pageindex-evaluation/fixed-suite.json`](../test-assets/pageindex-evaluation/fixed-suite.json)

The native build and tests ran on Windows 11 Enterprise LTSC x64,
10.0.26100, Windows PowerShell 5.1.26100.9168, an AMD Ryzen 7 9700X host with
6 reported logical processors and 12 GiB RAM. The host contains development
runtimes, so every PageIndex worker and Engine acceptance child receives a
restricted package-only `PATH`, supported offline switches, and loopback-only
proxy variables. The package is copied to a path containing spaces and non-ASCII
characters, and the bounded test harness invokes only package-local
executables. This proves offline package closure on the supplied host but is
not a substitute for the outstanding clean Windows 10 image row; that missing
row is recorded as a failed release gate, not as an inferred pass.

## Package and runtime result

The native Windows build emitted the versioned ZIP, SHA-256 sidecar, release
summary, schema-3 manifest, fixed WebView2 runtime, frozen Engine, and isolated
PageIndex onedir worker. The black-box test passed:

- PageIndex 0.2.10 self-check and real offline Markdown tree construction;
- missing-input failure with a non-zero exit, bounded process termination, and
  no orphan worker;
- a frozen-Engine disposable-KB matrix covering provider timeout, invalid tree,
  worker crash, and a rehashed corrupt private cache; every provider failure
  preserved the Available document, deterministic current tree, baseline
  retrieval, SQLite integrity, and foreign keys;
- Engine handshake, health, request cancellation, and clean shutdown;
- Markdown and scanned-PDF deterministic stages through Evidence and
  deterministic PageTree, followed by the required unconfigured-model
  quarantine rather than an invalid publication;
- packaged model-transport loading and a durable failure record against an
  intentionally unreachable local endpoint;
- Shell-owned process-tree termination.

Measured package/runtime values:

| Metric | Baseline | Candidate / delta |
| --- | ---: | ---: |
| ZIP bytes | 601,134,321 | 614,885,444 (+13,751,123) |
| Expanded bytes | 1,295,161,402 | 1,328,385,625 (+33,224,223) |
| PageIndex component | — | 27,334,114 bytes |
| Engine cold-start p95 | 299.742 ms | 309.969 ms (+10.227 ms) |
| Engine peak working set | 43,950,080 bytes | 44,068,864 bytes |
| PageIndex self-check | — | 172.071 ms |
| PageIndex first query | — | 167.785 ms |
| PageIndex query p95 | — | 172.222 ms |
| PageIndex peak working set | — | 35,520,512 bytes |

The cold-start delta is inside the one-second budget and confirms the provider
is not eagerly loaded into the Engine.

## Fixed Retrieval Evaluation result

The immutable `pageindex-portable-corpus-v1` suite covers local fact,
multi-hop, cross-document conflict, global theme, and absent answer across all
seven fixed variants. It used the exact packaged worker identity
`official_pageindex@0.2.10+ba0ef02d7803.openkb1` and reports
`fixed_suite_complete: true`. The candidate Engine validated the report against
the packaged manifest and fixed suite before the measurement script consumed
the gate. The audit record binds the five cases, seven variants, one
repetition, suite digest, provider identity, and report SHA-256
`c5e154102c13172cb45118aae5e475d5c8358bcd40124cbf8dc8f4d5f24f4c3e`;
the packaged corpus digest is
`6e92fd1f6eab474948d0930c2eef89ee8ecb3c968425cc010922575fa3766fca`.
The packaged worker SHA-256 is
`90fbd82b501e18fff6a7dd22cd09a23fefe9f5adaa80f5d2f8c89a8c5314a51c`.
The typed report records both identities from the actual evaluation run plus
its ending knowledge/derived snapshots. The validator recomputes metrics and
gates from case-level results and checks generation-reference closure; an
incomplete, stale, unrelated, or self-asserted report is a non-promotion
result.

The gate failed for substantive reasons:

- long-document Evidence Recall@6 was 1.0 for both baseline and PageIndex, so
  there was no required relative gain;
- PageIndex retrieval p95 was about 20.0 seconds, above the 10-second budget;
- four PageTree Selection runs degraded—three invalid model outputs and one
  failed timeout—so the selection-exercised and degradation-free conditions
  failed.

Citation precision, absent-answer comparison, faithfulness, snapshot stability,
derived-generation stability, provider identity binding, and the packaged
cold-start budget all stayed within their required boundaries. These passing
dimensions do not override the failed recall, latency, and degradation gates.
