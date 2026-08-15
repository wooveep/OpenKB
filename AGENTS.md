# AGENTS.md — OpenKB map for coding agents

OpenKB Desktop compiles locally imported documents into an evidence-backed
SQLite knowledge base. It uses vectorless retrieval and an optional local
knowledge graph. This repo is developed **agent-first**: humans steer, agents
execute. Optimize changes for agent legibility.

## Read next
- `docs/golden-principles.md` — mechanical rules to follow (enforced where possible).
- `docs/internal/superpowers/{specs,plans}/` — design history & plans *(maintainer-local, not in git)*.
- `README.md` — user-facing Desktop overview and packaging notes.

## Dev commands
- Install: `pip install -e ".[dev]"`  (or `uv sync --extra dev` — plain `uv sync` skips the dev tools)
- Run Engine: `uv run openkb-desktop-engine` (private stdio entry point)
- Test: `pytest`
- Lint/format/types: `ruff check .` · `ruff format .` · `mypy openkb`

## Module map (openkb/)
- `desktop_engine.py` — private stdio Engine and request dispatch.
- `desktop_workspace.py` — Desktop KB lifecycle and SQLite schema activation.
- `desktop_import_*.py` — staged import, parsing, deduplication, recovery, and quarantine.
- `desktop_*_parsers.py` — format-specific DocumentIR parsers and media extraction.
- `desktop_retrieval.py`, `desktop_grounded_answer.py` — evidence retrieval and grounded answers.
- `desktop_knowledge_*.py` — graph extraction, knowledge pages, reconciliation, and materialization.
- `desktop_model_*.py` — model gateway, settings, transport, and retry ledger.
- `desktop_raw_assets.py` — original-document reader and source-image access.
- `config.py` — KB-local, non-secret model configuration.
- `locks.py` — atomic writes and KB-local file locking.

## Hard invariants
- Deps are pinned **exactly** (supply-chain caution). Vet before bumping.
- Desktop KB writes go through `locks.py` and their owning service (never ad-hoc).
- Modules stay < 800 lines (`tests/test_file_size.py`); grandfathered files are in tech-debt.
- Keep this file a short map — put depth in `docs/`.


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
