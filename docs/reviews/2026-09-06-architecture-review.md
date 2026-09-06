# Repository review and reorganization — 2026-09-06

Baseline: `b243f61` (the full checkout, not a feature-branch diff).

The review covered the tracked Python Engine, React workbench, Rust Shell,
tests, evaluation tools, documentation, dependency manifests, CI, and packaging
entry points. Generated releases, installed dependencies, and maintainer-local
artifacts are outside the source review. Architecture findings were checked
against `AGENTS.md`, the golden principles, README, and ADRs; this is not a
claim that every application scenario has been exhaustively verified.

## Standards

### Fixed

| Finding | Result and evidence |
| --- | --- |
| 279 Python source files shared the package root; filenames encoded ownership with repeated prefixes. | 276 existing modules moved into domain packages; the root now contains three Python files. Imports and patch targets were updated directly. See [architecture](../architecture.md). |
| Frontend business UI, contracts, adapters, and controllers shared one flat directory. | 70 modules grouped into `app`, `bridge`, `features`, and `shared`. Reusable UI primitives retain their existing home. |
| Native commands, wire values, protocol methods, and runtime logic shared one source directory. | 41 existing modules moved into `commands`, `engine`, `runtime`, and `diagnostics`; `main.rs` now owns assembly. |
| P2: reanalysis polling invalidated every response when an RPC exceeded the one-second interval. | [Completion-driven polling](../../frontend/src/desktop/shared/polling.ts) is shared by the two polling hooks. Deferred-promise tests cover slow responses, transient failures, and disposal. |
| P2: an open document detail panel retained an obsolete task object. | [Document selection](../../frontend/src/desktop/features/documents/DesktopDocumentImportPanel.tsx) stores a job ID and derives the task from the current list. |
| P2: synchronous Tauri commands could start or wait for the Engine on the IPC executor. | [Shared command execution](../../desktop/src-tauri/src/commands/mod.rs) owns the blocking-pool transition. 58 existing asynchronous call sites and 10 formerly synchronous request paths use it. All 77 registered commands retain their names, parameters, return types, and Engine calls. |
| Frontend output was placed inside the Python package despite the Vite comment requiring separation. | Vite emits `frontend/dist`; Tauri consumes that location. The built Python wheel contains no workbench bundle. |
| Dependency ranges contradicted the exact-pin rule. | 62 frontend declarations now use their existing lockfile resolutions. No resolved dependency package or integrity entry changed. |
| CI lacked the primary Python and frontend checks. | [Source validation](../../.github/workflows/validate.yml) runs Python lint/types/tests and frontend lint/tests/build for all PRs and main pushes, including test-only changes. Windows packaging remains a separate job. |
| The historical design folder was misspelled. | `docs/desgin` moved to `docs/design`; the tracked ADR link was updated. |

### Remaining design debt

- The Tauri and memory adapters still use inheritance to distribute methods
  across domain files. Grouping them improves discoverability but does not
  remove that coupling. A future change should compose domain adapters around
  a shared transport, with contract parity tests for each implementation.
- The existing UI test runner still has source-string assertions. The new
  polling tests exercise behavior, but complete hook and detail-panel
  interactions need a component/browser harness.
- Python's existing build-system requirements (`hatchling`, `hatch-vcs`) remain
  unpinned. Choose and vet explicit build-tool versions separately; this change
  did not select new package versions.

## Spec and architecture constraints

### Fixed or preserved

| Finding / constraint | Result and evidence |
| --- | --- |
| Dozens of services repeated SQLite connection setup. | 41 initialization sites use [the connection policy](../../openkb/storage/sqlite.py). Caller-owned locks, transactions, connection closure, path validation, and custom busy timeouts are preserved. Read-only and backup policies remain distinct. |
| Durable timestamps had 31 equivalent implementations. | [The shared clock](../../openkb/shared/clock.py) retains the original UTC ISO format. Monotonic timers, log formatting, and lease durations keep their own semantics. |
| Current and legacy model recovery duplicated checkpoint deletion and plan identity lookup. | [Recovery storage](../../openkb/knowledge/analysis/recovery_store.py) owns both operations inside the caller's transaction. |
| Path-only consumers imported the Workspace runtime and migrations. | 45 import sites now depend directly on `workspace.paths`. |
| Engine handlers depended back on server-owned request types and validation. | [Protocol](../../openkb/engine/protocol.py) owns framing, request values, and parameter validation. Equivalent string/path checks share one implementation. Runtime handlers no longer import server implementation; type-only references remain. |
| Module-name-based diagnostic routing could break during relocation. | [Component mapping](../../openkb/diagnostics/settings.py) follows the new packages while retaining public component names. Regression tests cover parser and background-worker routing. |
| Frozen executable and isolated provider rely on physical source paths. | Console entry point, PyInstaller launcher, adjacent PageIndex worker, packaging commands, and test patch paths were updated. The wheel was imported from outside the checkout. |
| Persisted state and external Bridge contracts are authoritative. | No schema migration, data rewrite, prompt-policy change, or Bridge contract change was introduced. Canonical JSON and evidence/retry authority rules retain their implementations. |

### Remaining design debt

- `models.gateway` still combines value types and construction behavior, with
  deferred dependencies on lifecycle implementations. Package relocation
  intentionally preserves those construction semantics.
- ADR 0019 calls for generated cross-language contracts; current Python, Rust,
  and TypeScript values are maintained separately. The existing wire tests
  provide coverage, but a single schema/generator remains future work.

## Verification

- Before changes: 724 Python tests and 54 Rust tests passed; Python lint/types
  and frontend tests/build passed.
- Final Python run: 730 tests passed (724 original tests plus six new
  storage/architecture tests). Ruff and mypy passed.
- Rust: 57 tests passed, including worker-thread execution, domain error
  preservation, and panic-to-Bridge-error handling.
- Frontend: ESLint, UI/polling tests, and production build passed. The existing
  bundle-size and mixed static/dynamic import warnings remain.
- Python distribution: wheel build passed; all 305 importable modules loaded
  from the unpacked wheel outside the checkout. The console target and
  independent worker are present; old flat modules and web build assets are absent.
- The installed console entry point completed a framed handshake and shutdown
  outside the checkout. The existing CodeGraph index was refreshed and verified.
- Independent final reviews compared Python function/class implementations and
  the 77 Tauri command signatures/calls against the baseline. No new P1/P2
  regression was identified.

Windows portable packaging was reviewed statically, including source paths,
but no Windows frozen build or native GUI acceptance run was performed here.
Source layout changes require external development scripts using old private
`openkb.desktop_*` imports to adopt the new paths. The desktop command name,
Bridge methods, KB layout, and stored data remain unchanged.
