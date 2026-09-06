# Source architecture

OpenKB has three executables/build surfaces: the React workbench, the native
Tauri Shell, and the Python Engine. Directory names express ownership within
each surface. A feature change starts in its owning package; shared behavior
is extracted when multiple callers need the same invariant.

## Python Engine

| Package | Ownership and starting points |
| --- | --- |
| `openkb/engine/` | `protocol.py` frames and validates requests; `server.py` dispatches them; `entrypoint.py` starts the frozen executable. |
| `openkb/workspace/` | `runtime.py` creates/activates KBs; `paths.py` is the lightweight layout interface; migrations and backups remain here. |
| `openkb/importing/` | Staged import orchestration, job storage, deduplication, recovery, quarantine, and normalized import artifacts. |
| `openkb/parsers/` | Format-specific parsing and managed OCR/Office runtimes. |
| `openkb/documents/` | Original assets, source identity/integrity, document versions, lineage, and missing-source binding. |
| `openkb/models/` | Model settings, capability checks, transport, execution lifecycle, retry authority, and prompt contracts. |
| `openkb/page_tree/` | Document trees and enrichment; `pageindex/` owns the optional isolated provider. |
| `openkb/knowledge/` | `analysis/`, `corpus/`, `graph/`, `pages/`, `reanalysis/`, `reconciliation/`, and `export/` own their respective state and behavior. |
| `openkb/retrieval/` | Retrieval planning, catalogs, channels, fusion, and the bounded `navigation/` implementation. |
| `openkb/answers/` | Grounded answers, persistence, and conversations. |
| `openkb/diagnostics/` | Structured logging, configuration, failure context, sensitive traces, and diagnostic bundles. |
| `openkb/evaluation/` | Packaged retrieval/PageIndex acceptance and quality gates; runnable semantic experiments remain under repository-level `evaluation/`. |
| `openkb/storage/` | SQLite connection policy and read-only reporting connections. |
| `openkb/shared/` | Canonical JSON, UTC timestamps, and bounded parallel execution. |

The package root retains `__init__.py`, `config.py`, and `locks.py`. Class names,
Bridge method names, error codes, schema versions, and persisted identifiers
keep their established meanings even though source module paths changed.

### Interfaces and dependency direction

- Import request values and validation from `engine.protocol`. Handlers refer
  to `engine.server` only for type checking; dispatch owns runtime assembly.
- Import filesystem layout from `workspace.paths`. A caller that only needs a
  database path does not need the Workspace runtime or its migrations.
- `storage.sqlite.connect_database()` enables foreign keys and preserves the
  supplied SQLite busy timeout. Callers acquire the ingest lock, establish the
  transaction, and close the connection. Read-only reporting and backup
  connections retain their distinct policies.
- `shared.clock.timestamp()` serializes durable UTC state with `+00:00`.
  Elapsed durations still use monotonic clocks; leases retain their own policy.
- `knowledge.analysis.recovery_store` removes checkpoints in foreign-key order
  and reads historical plan identities within the caller's transaction. Both
  current and legacy recovery cross this same interface.
- Package initializers contain no eager service imports. Existing deferred
  imports remain where orchestration requires them. Domain packages are not
  claimed to form a completely acyclic dependency graph.

`tests/test_package_architecture.py` protects the root layout, foundational
dependencies, and protocol independence. `tests/test_file_size.py` recursively
checks all three production source trees.

## Frontend

`frontend/src/desktop/` contains four groups:

- `app/` assembles the workbench and owns active-KB/runtime integration.
- `bridge/` owns contracts, Tauri invocation, normalization, and memory or
  unavailable adapters. Contract types live in `bridge/contracts/`.
- `features/` contains documents, answers, knowledge, review, tasks, settings,
  diagnostics, and search. A feature keeps its UI and local controllers together.
- `shared/` contains request identifiers, source locators, refresh scheduling,
  and UI used across features. General UI primitives remain in `components/ui/`.

Imports across groups use the existing `@/` alias. Polling schedules the next
request after the current promise settles; `shared/polling.ts` owns this
policy. Keep selection state as identifiers and derive displayed records from
the current collection so background updates remain visible.

Vite emits `frontend/dist/`. Tauri embeds that directory. Frontend assets are
outside the Python distribution; `npm ci` uses the exactly pinned manifest and
lockfile versions.

## Native Shell

`desktop/src-tauri/src/main.rs` assembles Tauri, application state, and command
registration. Implementations live in:

- `commands/`: native commands; `run_engine()` performs blocking Engine work
  on the blocking pool and maps worker failures to a stable Bridge error.
- `engine/protocol/`: process supervision, private stdio, and domain requests.
- `engine/wire/`: serialized request/response values and their validation tests.
- `runtime/`: window/tray lifecycle, process-tree management, and external URLs.
- `diagnostics/`: Shell logging and its configuration.

Commands that can start or wait for the Engine use `run_engine()`. Native UI
work remains on the Tauri-supported path. Domain errors returned by the Engine
pass through unchanged; worker failures use `desktop_command_failed`.

## Entry points and relocation checklist

The command remains `openkb-desktop-engine`; its Python target is
`openkb.engine.server:main`. PyInstaller starts `openkb/engine/entrypoint.py`,
which imports that same module identity. The PageIndex provider builds from
`openkb/page_tree/pageindex/worker.py`; the source worker stays adjacent to its
adapter and can run independently in its isolated environment.

When relocating code, update static imports, string-based patch targets,
logging component prefixes, source-policy tests, and packaging scripts. Verify
the built wheel from outside the checkout, including the worker and console
entry point. Historical ADR/design prose may retain the paths used at the
time; this document describes the current layout.

For the findings behind this organization and remaining design work, see the
[September 2026 review](reviews/2026-09-06-architecture-review.md).

当前设计权威与历史替代关系见 [current-decisions.md](current-decisions.md)。
