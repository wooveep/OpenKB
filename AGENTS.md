# AGENTS.md — OpenKB map for coding agents

OpenKB compiles raw documents into an interlinked wiki knowledge base using
LLMs (vectorless retrieval via PageIndex). This repo is developed **agent-first**:
humans steer, agents execute. Optimize changes for agent legibility.

## Read next
- `docs/golden-principles.md` — mechanical rules to follow (enforced where possible).
- `docs/internal/superpowers/{specs,plans}/` — design history & plans *(maintainer-local, not in git)*.
- `README.md` — user-facing overview and commands.

## Dev commands
- Install: `pip install -e ".[dev]"`  (or `uv sync --extra dev` — plain `uv sync` skips the dev tools)
- Run CLI: `openkb <command>`  (entry point: `openkb.cli:cli`)
- Test: `pytest`
- Lint/format/types: `ruff check .` · `ruff format .` · `mypy openkb`

## Module map (openkb/)
- `cli.py` — Click CLI entry point & command wiring *(large; see tech-debt)*.
- `config.py` — config loading/validation (LiteLLM passthrough, env).
- `converter.py` — document → markdown conversion (markitdown).
- `url_ingest.py` — fetch & ingest URLs (trafilatura).
- `images.py` — figure/image extraction & handling.
- `indexer.py` — PageIndex tree indexing for long docs.
- `mutation.py` — crash-safe, serial KB mutations.
- `locks.py` — atomic writes / file locking (`atomic_write_text`, portalocker).
- `state.py` — run/session state tracking.
- `frontmatter.py` — YAML frontmatter round-trip (OKF).
- `schema.py` — page/content schema constants & helpers.
- `lint.py` — structural wiki lint (broken links, orphans, index sync).
- `tree_renderer.py`, `visualize.py`, `watcher.py` — rendering / graph / file watch.
- `agent/compiler.py` — LLM wiki compiler *(large; see tech-debt)*.
- `agent/linter.py` — semantic (LLM) wiki lint (contradictions, gaps, staleness).
- `agent/chat.py`, `agent/chat_session.py` — chat over the wiki *(chat.py large)*.
- `agent/query.py` — one-off query generator.
- `agent/tools.py` — shared wiki read/write tool functions used by query/linter (and by chat indirectly via `query.build_chat_agent`).
- `agent/skills.py`, `agent/skill_runner.py`, `skill/` — Skill Factory.
- `deck/`, `templates/`, `prompts/` — deck output, templates, prompt assets.

## Hard invariants
- Deps are pinned **exactly** (supply-chain caution). Vet before bumping.
- Wiki writes go through `locks.py` / `mutation.py` (never ad-hoc).
- Modules stay < 800 lines (`tests/test_file_size.py`); grandfathered files are in tech-debt.
- Keep this file a short map — put depth in `docs/`.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **OpenKB** (5067 symbols, 15885 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/OpenKB/context` | Codebase overview, check index freshness |
| `gitnexus://repo/OpenKB/clusters` | All functional areas |
| `gitnexus://repo/OpenKB/processes` | All execution flows |
| `gitnexus://repo/OpenKB/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Epistemic Task Routing

For each task, silently classify each subtask by what the user and agent know. Treat the classification as dynamic and update it when new evidence appears.

### Definitions

- **User-known:** The user provided the context, constraints, preferences, or business decision.
- **Agent-known:** The answer is supported by repository evidence, documentation, tools, tests, or reliable knowledge.
- **Unknown:** Evidence is insufficient. Never treat a guess as known.

### Routing

1. **Both know — Execute**
   - Act directly without low-value confirmation.
   - Follow existing project conventions.
   - Verify the result with relevant tests or checks.

2. **Agent knows, user does not — Explain**
   - Investigate reliable sources when needed.
   - Lead with the conclusion, then explain reasoning and tradeoffs.
   - Distinguish verified facts from inference or recommendations.

3. **Neither knows — Co-create**
   - State key unknowns and assumptions.
   - Propose materially different hypotheses or options.
   - Define evaluation criteria and prefer small, reversible experiments.
   - Do not present exploratory conclusions as facts.

4. **User knows, agent does not — Acquire context**
   - Inspect available files, code, logs, and prior context before asking.
   - Ask only the minimum specific questions needed.
   - Never invent private facts, preferences, APIs, or business rules.
   - Once context is sufficient, switch to direct execution.

### Decision Rules

- Classify mixed tasks per subtask, not as one quadrant.
- Use reasonable defaults for reversible, low-risk decisions.
- Ask before irreversible, high-risk, security-sensitive, costly, or scope-changing decisions.
- Explicitly label important assumptions and unresolved uncertainty.
- Reclassify the task whenever new context or evidence appears.
- Lead final responses with the outcome, followed by key changes, risks, and verification.
