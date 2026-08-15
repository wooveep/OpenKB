# Golden Principles

Opinionated, mechanical rules that keep this agent-generated codebase legible
and consistent for future agent runs. Enforced by CI where possible; the rest
are honored by convention and checked in review. When a rule proves valuable,
promote it into a lint (see `tests/test_file_size.py` for the pattern).

## Boundaries
- **Validate data shapes at boundaries.** Parse/validate Desktop Bridge frames,
  document IR, and configuration at the edge. Never build on guessed shapes.

## Reuse
- **Prefer shared utilities over hand-rolled helpers** so invariants stay
  centralized. Check `openkb/` for an existing helper before writing a new one.

## I/O and state
- **All Desktop KB mutations go through `openkb/locks.py` and the owning
  SQLite service** (atomic, crash-safe). No ad-hoc writes to a KB's state,
  raw assets, or generated pages.
- **Keep protocol stdout clean.** Python Engine diagnostics go through logging
  to stderr; stdout is reserved for framed Desktop Bridge messages.

## Size and shape
<a id="file-size"></a>
- **Keep modules focused and under 800 lines** (enforced by
  `tests/test_file_size.py`). Split large modules into focused units by
  responsibility. Existing over-limit files are grandfathered (with reasons)
  in the test's `_GRANDFATHERED` set and additionally tracked in
  `docs/internal/tech-debt.md` *(maintainer-local, not in git)*.

## Docs
- **`AGENTS.md` is a map, not a manual.** Keep it short; deep/local docs live
  under `docs/` (public) and `docs/internal/` (maintainer-local, not in git).
