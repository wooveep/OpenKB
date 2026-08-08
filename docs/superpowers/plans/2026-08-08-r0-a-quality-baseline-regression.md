# R0-A Quality Baseline and Regression Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, evidence-aware harness that freezes the current OpenKB corpus, captures the existing PageIndex/Wiki Q&A baseline, and blocks future releases when retrieval or answer quality regresses.

**Architecture:** Add an isolated `openkb.evaluation` package around the existing `build_query_agent()` and `iter_agent_response_events()` boundary without changing current query behavior. A frozen JSON suite binds reviewed questions to a deterministic corpus fingerprint; resumable run manifests capture answers and normalized evidence calls; deterministic and blind pairwise comparisons emit machine-readable and Markdown release gates.

**Tech Stack:** Python standard library dataclasses/JSON/hashlib/asyncio, Click 8.4.0, openai-agents 0.17.3, LiteLLM 1.87.2, json-repair 0.59.10, pytest 9.0.3, pytest-asyncio 1.3.0, existing atomic-write utilities.

## Global Constraints

- Source of truth: `docs/superpowers/specs/2026-08-08-openkb-local-knowledge-graph-desktop-design.md`, especially sections 5, 9, 16, and 18.
- Scope is R0-A only. Do not implement SQLite authority, CAS, Stage DAG, Model Gateway retries, knowledge graph, desktop GUI, migration, or WebUI removal.
- Preserve the current PageIndex/Wiki prompt, tools, and answer path. R0-A observes `build_query_agent()` and `iter_agent_response_events()`; it does not replace them.
- Formal Windows acceptance is Windows 10/11 64-bit with Python 3.12 64-bit. Do not narrow the repository's current Python `>=3.10` declaration in R0-A.
- Add no dependency. Existing dependencies remain pinned exactly.
- Automated tests make zero network or billable LLM calls; patch `Runner.run` or inject fakes.
- Store suites, runs, and reports below `<kb>/.openkb/evaluations/`; never write evaluation output into `wiki/`.
- Use `atomic_write_json` or `atomic_write_text` for every evaluation artifact.
- Never persist keys, request headers, full tool outputs, or request bodies. Persist hashes, evidence locators, answers, timings, statuses, and short errors only.
- Keep every new Python module below 800 physical lines.
- Before editing an existing symbol, run upstream GitNexus impact analysis. Warn and stop on HIGH/CRITICAL until the user approves.
- Before each commit: stage only task files, run `gitnexus detect-changes --repo OpenKB --scope staged`, review it, then run `git diff --cached --check`.
- After each commit run `node .gitnexus/run.cjs analyze`.
- Execute in a worktree created through `superpowers:using-git-worktrees`; the root checkout contains unrelated user changes.

## File Map

| File | Responsibility |
|---|---|
| `openkb/evaluation/schema.py` | Versioned dataclasses, JSON validation, canonical serialization and hashes. |
| `openkb/evaluation/fingerprint.py` | Fingerprint current query-visible files and freeze a suite. |
| `openkb/evaluation/evidence.py` | Normalize retrieval tool calls into comparable evidence locators. |
| `openkb/evaluation/runner.py` | Existing-agent adapter, repeated execution, checkpoints and resume. |
| `openkb/evaluation/metrics.py` | Recall@K, compatibility checks and strict evidence gate. |
| `openkb/evaluation/judge.py` | Bounded evidence excerpts and deterministic blind pairwise judging. |
| `openkb/evaluation/report.py` | Overall gate plus JSON/Markdown reports. |
| `openkb/evaluation/cli.py` | `quality freeze`, `run`, and `compare`. |
| `openkb/cli.py` | One command-registration call only. |
| `tests/fixtures/evaluation/` | Reviewed mini question set and deterministic Wiki fixture. |
| `tests/test_evaluation_*.py` | Focused unit/integration tests. |
| `tests/test_quality_cli.py` | Click contracts and exit codes. |
| `README.md` | Operator workflow and question-set schema. |

## Shared Contracts

Use these names and field types exactly:

```python
EvidenceKind = Literal["wiki_file", "page_range", "image"]
ObservationStatus = Literal["success", "error"]
RunStatus = Literal["running", "complete", "partial"]
GateStatus = Literal["pass", "fail", "inconclusive"]

@dataclass(frozen=True)
class EvidenceLocator:
    kind: EvidenceKind
    path: str | None = None
    document: str | None = None
    pages: tuple[int, ...] = ()

@dataclass(frozen=True)
class EvidenceExpectation:
    any_of: tuple[EvidenceLocator, ...]

@dataclass(frozen=True)
class QuestionCase:
    case_id: str
    question: str
    critical_evidence: tuple[EvidenceExpectation, ...]
    reference_points: tuple[str, ...]
    tags: tuple[str, ...] = ()

@dataclass(frozen=True)
class QuestionSet:
    schema_version: int
    name: str
    language: str
    repeats: int
    retrieval_cutoffs: tuple[int, ...]
    cases: tuple[QuestionCase, ...]

@dataclass(frozen=True)
class CorpusEntry:
    path: str
    size: int
    sha256: str

@dataclass(frozen=True)
class CorpusSnapshot:
    algorithm: Literal["sha256"]
    digest: str
    files: tuple[CorpusEntry, ...]

@dataclass(frozen=True)
class QualitySuite:
    schema_version: int
    questions: QuestionSet
    corpus: CorpusSnapshot

@dataclass(frozen=True)
class RetrievedEvidence:
    locator: EvidenceLocator
    rank: int

@dataclass(frozen=True)
class QueryObservation:
    case_id: str
    repeat_index: int
    status: ObservationStatus
    answer: str
    evidence: tuple[RetrievedEvidence, ...]
    latency_ms: int
    error: str | None = None
    warnings: tuple[str, ...] = ()

@dataclass(frozen=True)
class RunProfile:
    model: str
    language: str
    max_turns: int
    model_settings_sha256: str
    provider_sha256: str
    prompt_sha256: str
    openkb_version: str

@dataclass(frozen=True)
class RunManifest:
    schema_version: int
    run_id: str
    label: str
    suite_sha256: str
    corpus_sha256: str
    profile: RunProfile
    started_at: str
    finished_at: str | None
    status: RunStatus
    observations: tuple[QueryObservation, ...]
```

All R0-A artifacts use `schema_version=1`. `repeats >= 3`. Retrieval cutoffs are sorted unique positive integers. Every case has a unique lowercase ID, a non-empty question, at least one evidence expectation, and at least one reviewed reference point. Every expectation contains at least one `wiki_file` or `page_range` alternative; image-only evidence cannot support a text-only judge.

---

### Task 1: Versioned evaluation schemas

**Files:**
- Create: `openkb/evaluation/__init__.py`
- Create: `openkb/evaluation/schema.py`
- Create: `tests/fixtures/evaluation/questions-v1.json`
- Create: `tests/test_evaluation_schema.py`

**Interfaces:**
- Consumes: standard-library JSON/dataclasses/path/types only.
- Produces: shared dataclasses; `SchemaError`; `validate_relative_posix_path`; `validate_document_name`; `load_question_set(Path) -> QuestionSet`; `load_suite(Path) -> QualitySuite`; `question_set_to_dict`; `suite_to_dict`; `canonical_sha256`; `suite_sha256`.

- [ ] **Step 1: Add a valid reviewed question-set fixture**

```json
{
  "schema_version": 1,
  "name": "r0-a-mini",
  "language": "en",
  "repeats": 3,
  "retrieval_cutoffs": [1, 3, 5],
  "cases": [
    {
      "id": "alpha-owner",
      "question": "Who owns Project Alpha?",
      "critical_evidence": [{"any_of": [
        {"kind": "wiki_file", "path": "entities/alice.md"},
        {"kind": "page_range", "document": "alpha", "pages": [2]}
      ]}],
      "reference_points": ["Alice owns Project Alpha."],
      "tags": ["entity", "pageindex"]
    }
  ]
}
```

- [ ] **Step 2: Write failing boundary tests**

```python
from pathlib import Path
import json
import pytest
from openkb.evaluation.schema import SchemaError, load_question_set

FIXTURE = Path("tests/fixtures/evaluation/questions-v1.json")

def test_load_question_set_v1():
    value = load_question_set(FIXTURE)
    assert value.repeats == 3
    assert value.cases[0].critical_evidence[0].any_of[1].pages == (2,)

def test_rejects_duplicate_ids(tmp_path):
    data = json.loads(FIXTURE.read_text())
    data["cases"].append(dict(data["cases"][0]))
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(data))
    with pytest.raises(SchemaError, match="unique"):
        load_question_set(path)

@pytest.mark.parametrize("unsafe", ["../secret.md", "/absolute.md", "C:/secret.md"])
def test_rejects_unsafe_paths(tmp_path, unsafe):
    data = json.loads(FIXTURE.read_text())
    data["cases"][0]["critical_evidence"][0]["any_of"][0]["path"] = unsafe
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(data))
    with pytest.raises(SchemaError, match="relative POSIX path"):
        load_question_set(path)
```

- [ ] **Step 3: Verify RED**

Run: `pytest tests/test_evaluation_schema.py -v`

Expected: collection fails because `openkb.evaluation.schema` does not exist.

- [ ] **Step 4: Implement strict parsing and canonical serialization**

```python
class SchemaError(ValueError):
    """An evaluation artifact failed boundary validation."""

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SchemaError(message)

def validate_relative_posix_path(value: object) -> str:
    _require(isinstance(value, str) and bool(value), "evidence path must be non-empty")
    path = PurePosixPath(value)
    _require(
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and re.match(r"^[A-Za-z]:", value) is None,
        "evidence path must be a relative POSIX path",
    )
    return value

def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

Implement `_parse_locator`, `_parse_expectation`, and `_parse_case`. Reject unknown kinds, kind-inappropriate fields, empty alternatives, image-only expectations, non-positive pages, blank reference points, and IDs outside `^[a-z0-9][a-z0-9_-]{0,63}$`. `load_question_set` enforces all shared rules and duplicate IDs. Serializers emit JSON lists and omit non-applicable locator fields. `load_suite` validates that file rows are sorted/unique and that the stored corpus digest equals their canonical digest.

`validate_document_name` accepts non-empty Unicode document names up to 255 characters but rejects `/`, `\\`, `..`, and control characters. `suite_sha256` is `canonical_sha256(suite_to_dict(suite))`.

- [ ] **Step 5: Verify GREEN**

Run: `pytest tests/test_evaluation_schema.py -v`

Expected: all pass.

Run: `ruff check openkb/evaluation/schema.py tests/test_evaluation_schema.py`

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add openkb/evaluation/__init__.py openkb/evaluation/schema.py tests/fixtures/evaluation/questions-v1.json tests/test_evaluation_schema.py
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "feat(evaluation): add versioned quality schemas"
node .gitnexus/run.cjs analyze
```

### Task 2: Deterministic corpus fingerprint and suite freeze

**Files:**
- Create: `openkb/evaluation/fingerprint.py`
- Create: `tests/fixtures/evaluation/wiki/AGENTS.md`
- Create: `tests/fixtures/evaluation/wiki/index.md`
- Create: `tests/fixtures/evaluation/wiki/entities/alice.md`
- Create: `tests/fixtures/evaluation/wiki/sources/alpha.json`
- Create: `tests/test_evaluation_fingerprint.py`

**Interfaces:**
- Consumes: question/corpus/suite contracts and existing `atomic_write_json`.
- Produces: `fingerprint_kb(Path) -> CorpusSnapshot`; `freeze_suite(Path, QuestionSet) -> QualitySuite`; `save_suite(Path, QualitySuite) -> None`.

- [ ] **Step 1: Add the mini Wiki fixture**

`AGENTS.md`:

```markdown
# Evaluation Wiki

Read index.md, then follow entity and source pages.
```

`index.md`:

```markdown
# Knowledge Base Index

- [[entities/alice|Alice]]
- Source document: alpha (pageindex)
```

`entities/alice.md`:

```markdown
# Alice

Alice owns Project Alpha.
```

`sources/alpha.json`:

```json
[
  {"page": 1, "content": "Project Alpha overview."},
  {"page": 2, "content": "Alice owns Project Alpha."}
]
```

- [ ] **Step 2: Write failing fingerprint tests**

```python
from pathlib import Path
from shutil import copytree
from openkb.evaluation.fingerprint import fingerprint_kb, freeze_suite
from openkb.evaluation.schema import load_question_set

ROOT = Path("tests/fixtures/evaluation")

def test_volatile_files_do_not_change_digest(tmp_path):
    copytree(ROOT / "wiki", tmp_path / "wiki")
    first = fingerprint_kb(tmp_path)
    (tmp_path / "wiki" / "log.md").write_text("volatile")
    (tmp_path / "wiki" / "explorations").mkdir()
    (tmp_path / "wiki" / "explorations" / "x.md").write_text("volatile")
    assert fingerprint_kb(tmp_path).digest == first.digest

def test_query_visible_change_changes_digest(tmp_path):
    copytree(ROOT / "wiki", tmp_path / "wiki")
    first = fingerprint_kb(tmp_path)
    target = tmp_path / "wiki" / "entities" / "alice.md"
    target.write_text(target.read_text() + "\nChanged.\n")
    assert fingerprint_kb(tmp_path).digest != first.digest

def test_freeze_binds_questions_and_corpus(tmp_path):
    copytree(ROOT / "wiki", tmp_path / "wiki")
    suite = freeze_suite(tmp_path, load_question_set(ROOT / "questions-v1.json"))
    assert suite.corpus.digest == fingerprint_kb(tmp_path).digest
```

- [ ] **Step 3: Verify RED**

Run: `pytest tests/test_evaluation_fingerprint.py -v`

Expected: module import fails.

- [ ] **Step 4: Implement deterministic file selection and hashing**

Only include `wiki/AGENTS.md`, `wiki/index.md`, and regular files below `wiki/summaries`, `concepts`, `entities`, and `sources`. Exclude `log.md`, `explorations`, `reports`, `.openkb`, and all symlinks.

```python
QUERY_ROOT_FILES = ("AGENTS.md", "index.md")
QUERY_DIRECTORIES = ("summaries", "concepts", "entities", "sources")

def fingerprint_kb(kb_dir: Path) -> CorpusSnapshot:
    wiki = (kb_dir / "wiki").resolve()
    paths = [wiki / name for name in QUERY_ROOT_FILES if (wiki / name).is_file()]
    for directory in QUERY_DIRECTORIES:
        root = wiki / directory
        if root.is_dir():
            paths.extend(path for path in root.rglob("*") if path.is_file())
    if any(path.is_symlink() for path in paths):
        raise ValueError("evaluation corpus must not contain symlinks")
    entries = tuple(
        CorpusEntry(
            path=path.relative_to(wiki).as_posix(),
            size=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(paths, key=lambda item: item.relative_to(wiki).as_posix())
    )
    digest = canonical_sha256([asdict(entry) for entry in entries])
    return CorpusSnapshot("sha256", digest, entries)

def freeze_suite(kb_dir: Path, questions: QuestionSet) -> QualitySuite:
    return QualitySuite(1, questions, fingerprint_kb(kb_dir))

def save_suite(path: Path, suite: QualitySuite) -> None:
    atomic_write_json(path, suite_to_dict(suite), ensure_ascii=False)
```

- [ ] **Step 5: Verify GREEN and commit**

Run: `pytest tests/test_evaluation_schema.py tests/test_evaluation_fingerprint.py -v`

Expected: all pass.

```bash
git add openkb/evaluation/fingerprint.py tests/fixtures/evaluation/wiki tests/test_evaluation_fingerprint.py
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "feat(evaluation): freeze query-visible corpus"
node .gitnexus/run.cjs analyze
```

### Task 3: Normalize retrieval evidence

**Files:**
- Create: `openkb/evaluation/evidence.py`
- Create: `tests/test_evaluation_evidence.py`

**Interfaces:**
- Consumes: evidence contracts and existing `openkb.agent.tools.parse_pages`.
- Produces: `EvidenceParseError`; `normalize_tool_call(name: str, arguments: str, rank: int) -> RetrievedEvidence | None`.

- [ ] **Step 1: Write failing tests**

```python
import pytest
from openkb.evaluation.evidence import EvidenceParseError, normalize_tool_call

def test_normalizes_pageindex_call():
    hit = normalize_tool_call(
        "get_page_content", '{"doc_name":"alpha","pages":"2-3,5"}', 2
    )
    assert hit.locator.document == "alpha"
    assert hit.locator.pages == (2, 3, 5)
    assert hit.rank == 2

def test_normalizes_wiki_file():
    hit = normalize_tool_call("read_file", '{"path":"entities/alice.md"}', 1)
    assert hit.locator.kind == "wiki_file"

def test_ignores_non_retrieval_tool():
    assert normalize_tool_call("write_file", '{"path":"output/x.md"}', 1) is None

def test_rejects_malformed_arguments():
    with pytest.raises(EvidenceParseError, match="get_page_content"):
        normalize_tool_call("get_page_content", "not-json", 1)
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_evaluation_evidence.py -v`

Expected: module import fails.

- [ ] **Step 3: Implement strict normalization**

```python
RETRIEVAL_TOOLS = {"read_file", "get_page_content", "get_image"}

class EvidenceParseError(ValueError):
    """A retrieval tool call could not be normalized."""

def normalize_tool_call(name: str, arguments: str, rank: int) -> RetrievedEvidence | None:
    if name not in RETRIEVAL_TOOLS:
        return None
    if rank < 1:
        raise EvidenceParseError("retrieval rank must be positive")
    try:
        data = json.loads(arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        raise EvidenceParseError(f"{name} arguments are not valid JSON") from exc
    if name == "get_page_content":
        document = data.get("doc_name")
        pages = tuple(parse_pages(str(data.get("pages", ""))))
        if not isinstance(document, str) or not document.strip() or not pages:
            raise EvidenceParseError("get_page_content requires doc_name and pages")
        locator = EvidenceLocator("page_range", document=document.strip(), pages=pages)
    else:
        key = "path" if name == "read_file" else "image_path"
        path = data.get(key)
        if not isinstance(path, str) or not path.strip():
            raise EvidenceParseError(f"{name} requires {key}")
        validate_relative_posix_path(path.strip())
        kind = "wiki_file" if name == "read_file" else "image"
        locator = EvidenceLocator(kind, path=path.strip())
    return RetrievedEvidence(locator, rank)
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `pytest tests/test_evaluation_evidence.py tests/test_agent_tools.py -v`

Expected: all pass.

```bash
git add openkb/evaluation/evidence.py tests/test_evaluation_evidence.py
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "feat(evaluation): normalize query evidence"
node .gitnexus/run.cjs analyze
```

### Task 4: Capture resumable baseline runs through the existing query agent

**Files:**
- Create: `openkb/evaluation/runner.py`
- Modify: `openkb/evaluation/schema.py`
- Modify: `openkb/evaluation/__init__.py`
- Create: `tests/test_evaluation_runner.py`

**Interfaces:**
- Consumes: `build_query_agent`, `iter_agent_response_events`, `MAX_TURNS`, evidence normalization, corpus fingerprint and shared run contracts.
- Produces: `QueryExecutor` protocol; `BaselineQueryExecutor`; `run_suite(suite: QualitySuite, *, label: str, run_path: Path, executor: QueryExecutor, resume: bool = False) -> RunManifest`; `load_run(Path) -> RunManifest`; `save_run(Path, RunManifest) -> None`; `run_to_dict`.

- [ ] **Step 1: Write failing checkpoint/resume tests**

```python
from dataclasses import replace
import pytest
from openkb.evaluation.runner import run_suite, save_run
from openkb.evaluation.schema import QueryObservation, RunProfile

class FakeExecutor:
    profile = RunProfile(
        "fake/model", "en", 50, "settings", "provider", "prompt", "test"
    )

    def __init__(self, corpus_sha256):
        self.corpus_sha256 = corpus_sha256
        self.calls = []

    async def run(self, case, repeat_index):
        self.calls.append((case.case_id, repeat_index))
        return QueryObservation(
            case.case_id, repeat_index, "success",
            "Alice owns Project Alpha.", (), 5,
        )

@pytest.mark.asyncio
async def test_run_writes_every_repeat(tmp_path, frozen_suite):
    executor = FakeExecutor(frozen_suite.corpus.digest)
    path = tmp_path / "run.json"
    result = await run_suite(
        frozen_suite, label="baseline", run_path=path, executor=executor
    )
    assert result.status == "complete"
    assert len(result.observations) == 3
    assert path.is_file()

@pytest.mark.asyncio
async def test_resume_skips_success_and_retries_error(tmp_path, frozen_suite):
    executor = FakeExecutor(frozen_suite.corpus.digest)
    path = tmp_path / "run.json"
    first = await run_suite(
        frozen_suite, label="baseline", run_path=path, executor=executor
    )
    observations = list(first.observations)
    observations[0] = replace(observations[0], status="error", error="timeout")
    save_run(path, replace(first, status="partial", observations=tuple(observations)))
    executor.calls.clear()
    await run_suite(
        frozen_suite, label="baseline", run_path=path,
        executor=executor, resume=True,
    )
    assert executor.calls == [(frozen_suite.questions.cases[0].case_id, 0)]
```

Define the `frozen_suite` fixture in this test by copying the checked-in Wiki, loading the question fixture, and calling `freeze_suite`.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_evaluation_runner.py -v`

Expected: module import fails.

- [ ] **Step 3: Implement the existing-agent adapter**

```python
class QueryExecutor(Protocol):
    profile: RunProfile
    corpus_sha256: str

    async def run(self, case: QuestionCase, repeat_index: int) -> QueryObservation:
        """Execute one reviewed question without mutating the KB."""

class BaselineQueryExecutor:
    def __init__(self, kb_dir, model, language, *, bundle=None, run_config=None):
        self._agent = build_query_agent(
            str(kb_dir / "wiki"), model, language, bundle=bundle
        )
        self._run_config = run_config
        self.corpus_sha256 = fingerprint_kb(kb_dir).digest
        self.profile = _profile_for_agent(self._agent, model, language, bundle)

    async def run(self, case, repeat_index):
        started = time.perf_counter()
        evidence = []
        warnings = []
        answer = ""
        try:
            async for event in iter_agent_response_events(
                self._agent, case.question,
                max_turns=MAX_TURNS, run_config=self._run_config,
            ):
                if event["event"] == "tool_call":
                    data = event["data"]
                    try:
                        hit = normalize_tool_call(
                            data["name"], data["arguments"], len(evidence) + 1
                        )
                    except EvidenceParseError as exc:
                        warnings.append(str(exc))
                    else:
                        if hit is not None:
                            evidence.append(hit)
                elif event["event"] == "final":
                    answer = str(event["data"].get("answer", "")).strip()
        except Exception as exc:
            return QueryObservation(
                case.case_id, repeat_index, "error", "", tuple(evidence),
                round((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}", warnings=tuple(warnings),
            )
        return QueryObservation(
            case.case_id, repeat_index, "success", answer, tuple(evidence),
            round((time.perf_counter() - started) * 1000),
            warnings=tuple(warnings),
        )
```

`_profile_for_agent` hashes exact instructions and sanitized `dataclasses.asdict(agent.model_settings)`. Remove `extra_headers`; remove only `timeout` from `extra_args`; retain generation settings. Hash the configured provider `base_url` into `provider_sha256` without storing the URL. Store `openkb.__version__`, not a shell-derived commit. Never store the `history` field from final events.

- [ ] **Step 4: Implement sequential execution and atomic resume**

Before the first model call, verify the live corpus fingerprint equals `suite.corpus.digest`. For each suite-ordered `(case_id, repeat_index)`, skip an existing success, retry an existing error, replace by key, sort deterministically, and atomically save after each observation. Do not add model retry logic in R0-A.

```python
async def run_suite(suite, *, label, run_path, executor, resume=False):
    if executor.corpus_sha256 != suite.corpus.digest:
        raise ValueError("live corpus does not match the frozen suite")
    existing = load_run(run_path) if resume else None
    _validate_resume(existing, suite, label, executor.profile)
    run = existing or _new_run(suite, label, executor.profile)
    by_key = {(item.case_id, item.repeat_index): item for item in run.observations}
    for case in suite.questions.cases:
        for repeat_index in range(suite.questions.repeats):
            key = (case.case_id, repeat_index)
            if key in by_key and by_key[key].status == "success":
                continue
            by_key[key] = await executor.run(case, repeat_index)
            run = _replace_observations(run, suite, by_key, status="running")
            save_run(run_path, run)
    complete = all(item.status == "success" for item in by_key.values())
    status = "complete" if complete else "partial"
    run = replace(
        _replace_observations(run, suite, by_key, status=status),
        finished_at=_utc_now(),
    )
    save_run(run_path, run)
    return run
```

- [ ] **Step 5: Test the real event boundary**

Patch `openkb.evaluation.runner.iter_agent_response_events` with an async generator yielding one `get_page_content` call and one final event. Assert the adapter returns the answer and rank-1 evidence, but not history or tool output.

- [ ] **Step 6: Verify GREEN and commit**

Run: `pytest tests/test_evaluation_runner.py tests/test_query.py -v`

Expected: all pass and query behavior remains unchanged.

```bash
git add openkb/evaluation/__init__.py openkb/evaluation/schema.py openkb/evaluation/runner.py tests/test_evaluation_runner.py
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "feat(evaluation): capture resumable baseline runs"
node .gitnexus/run.cjs analyze
```

### Task 5: Deterministic retrieval comparison and release gate

**Files:**
- Create: `openkb/evaluation/metrics.py`
- Create: `tests/test_evaluation_metrics.py`

**Interfaces:**
- Consumes: suite, run, expectation and retrieved-evidence contracts.
- Produces: `CaseRecall`, `RunScores`, `DeterministicComparison`; `score_run`; `compare_deterministic`.

```python
@dataclass(frozen=True)
class CaseRecall:
    case_id: str
    by_cutoff: dict[int, float]

@dataclass(frozen=True)
class RunScores:
    by_case: tuple[CaseRecall, ...]
    aggregate_recall: dict[int, float]

@dataclass(frozen=True)
class DeterministicComparison:
    status: GateStatus
    reasons: tuple[str, ...]
    baseline: RunScores
    candidate: RunScores
```

- [ ] **Step 1: Write failing recall and compatibility tests**

```python
from dataclasses import replace
from openkb.evaluation.metrics import compare_deterministic, score_run

def test_page_expectation_uses_union_within_cutoff(suite, complete_run):
    assert score_run(suite, complete_run).aggregate_recall[5] == 1.0

def test_lost_critical_evidence_fails(suite, complete_run):
    observations = list(complete_run.observations)
    observations[0] = replace(observations[0], evidence=())
    candidate = replace(
        complete_run, label="candidate", observations=tuple(observations)
    )
    result = compare_deterministic(suite, complete_run, candidate)
    assert result.status == "fail"
    assert "critical evidence regression" in result.reasons[0]

def test_incomplete_candidate_is_inconclusive(suite, complete_run):
    candidate = replace(complete_run, status="partial", observations=())
    assert compare_deterministic(suite, complete_run, candidate).status == "inconclusive"

def test_prompt_may_change_but_model_may_not(suite, complete_run):
    candidate = replace(
        complete_run, label="candidate",
        profile=replace(complete_run.profile, prompt_sha256="new-prompt"),
    )
    assert compare_deterministic(suite, complete_run, candidate).status == "pass"
    other_model = replace(
        candidate, profile=replace(candidate.profile, model="other/model")
    )
    assert compare_deterministic(suite, complete_run, other_model).status == "inconclusive"
```

In `tests/test_evaluation_metrics.py`, build `suite` with `freeze_suite` over the mini Wiki. Build `complete_run` with three successful observations, each containing rank-1 `EvidenceLocator("page_range", document="alpha", pages=(2,))`, and a profile whose suite/corpus hashes match that suite.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_evaluation_metrics.py -v`

Expected: module import fails.

- [ ] **Step 3: Implement Recall@K**

```python
def expectation_met(expectation, hits, cutoff):
    visible = [hit.locator for hit in hits if hit.rank <= cutoff]
    for expected in expectation.any_of:
        if expected.kind in {"wiki_file", "image"}:
            if any(
                hit.kind == expected.kind and hit.path == expected.path
                for hit in visible
            ):
                return True
        if expected.kind == "page_range":
            pages = {
                page
                for hit in visible
                if hit.kind == "page_range" and hit.document == expected.document
                for page in hit.pages
            }
            if set(expected.pages).issubset(pages):
                return True
    return False
```

Compute each observation's met-expectation fraction, average repeats per case, then average cases with equal weight. Do not weight by expectation count.

- [ ] **Step 4: Implement compatibility and strict gate rules**

Return `inconclusive` if suite hash, corpus hash, model, language, max turns, sanitized model-settings hash, provider hash, repeat count, expected observation keys, successful status, or non-empty answers differ. Prompt hash, label, OpenKB version and latency may differ. Compatible candidates must have per-case and aggregate recall greater than or equal to baseline at every cutoff; any decrease is `fail`.

- [ ] **Step 5: Verify GREEN and commit**

Run: `pytest tests/test_evaluation_metrics.py tests/test_evaluation_runner.py -v`

Expected: all pass.

```bash
git add openkb/evaluation/metrics.py tests/test_evaluation_metrics.py
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "feat(evaluation): gate retrieval regressions"
node .gitnexus/run.cjs analyze
```

### Task 6: Blind pairwise answer-quality judge

**Files:**
- Create: `openkb/evaluation/judge.py`
- Create: `tests/test_evaluation_judge.py`

**Interfaces:**
- Consumes: compatible suite/runs, existing read-only wiki tools, Agents SDK, current header/timeout helpers.
- Produces: `PairwiseVerdict`, `JudgeReport`; `load_evidence_excerpt`; `blind_assignment`; `judge_pair`; `run_pairwise_judging`.

```python
JudgeWinner = Literal["baseline", "candidate", "tie"]

@dataclass(frozen=True)
class JudgeProfile:
    model: str
    model_settings_sha256: str
    provider_sha256: str
    prompt_sha256: str

@dataclass(frozen=True)
class PairwiseVerdict:
    case_id: str
    repeat_index: int
    dimensions: dict[str, JudgeWinner]
    reason: str
    error: str | None = None

@dataclass(frozen=True)
class JudgeReport:
    profile: JudgeProfile
    verdicts: tuple[PairwiseVerdict, ...]
    errors: tuple[str, ...]
```

This profile proves one judge configuration evaluated both answers.

- [ ] **Step 1: Write failing blinding and parsing tests**

```python
from unittest.mock import AsyncMock, patch
import pytest
from openkb.evaluation.judge import blind_assignment, judge_pair

def test_assignment_is_deterministic():
    assert blind_assignment("alpha-owner", 0) == blind_assignment("alpha-owner", 0)

@pytest.mark.asyncio
async def test_maps_a_back_to_candidate(pair_input):
    result = type("Result", (), {
        "final_output": (
            '{"correctness":"A","completeness":"TIE",'
            '"citation_quality":"B","unsupported_assertions":"A",'
            '"reason":"A is more grounded."}'
        )
    })()
    with (
        patch("openkb.evaluation.judge.blind_assignment", return_value="candidate_is_a"),
        patch("openkb.evaluation.judge.Runner.run", new=AsyncMock(return_value=result)),
    ):
        verdict = await judge_pair(**pair_input)
    assert verdict.dimensions["correctness"] == "candidate"
    assert verdict.dimensions["citation_quality"] == "baseline"
```

`pair_input` is a dictionary fixture containing the mini case, repeat index `0`, the same-corpus baseline/candidate observations, mini KB path, judge model `fake/judge`, and a deterministic fake JudgeProfile. Do not hide any other input inside process globals.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_evaluation_judge.py -v`

Expected: module import fails.

- [ ] **Step 3: Implement bounded evidence loading**

Set per-item limit `8_000` characters and total limit `40_000`. Resolve only with `read_wiki_file` or `get_wiki_page_content`; image evidence contributes locator text, never base64. Build the deduplicated union of baseline/candidate evidence through the largest suite cutoff.

```python
def load_evidence_excerpt(kb_dir, locator):
    wiki_root = str(kb_dir / "wiki")
    if locator.kind == "wiki_file":
        text = read_wiki_file(locator.path or "", wiki_root)
    elif locator.kind == "page_range":
        pages = ",".join(str(page) for page in locator.pages)
        text = get_wiki_page_content(locator.document or "", pages, wiki_root)
    else:
        text = f"[Image evidence: {locator.path}]"
    return text[:8_000]
```

Treat `File not found`, `No content found`, `Access denied`, malformed source JSON, or an unreadable locator as `EvidenceLoadError`; preserve the error and make the judge report inconclusive. The schema's text-alternative rule ensures an image can supplement but never solely ground an evaluated fact.

- [ ] **Step 4: Implement deterministic blind judging**

```python
JUDGE_DIMENSIONS = (
    "correctness", "completeness",
    "citation_quality", "unsupported_assertions",
)

def blind_assignment(case_id, repeat_index):
    digest = hashlib.sha256(f"{case_id}:{repeat_index}".encode()).digest()
    return "baseline_is_a" if digest[0] % 2 == 0 else "candidate_is_a"
```

The judge sees the question, reference points, evidence, and answers A/B. For `unsupported_assertions`, the winner is the answer with fewer unsupported factual assertions. Require JSON with four values exactly `A`, `B`, or `TIE`, plus a one-sentence reason. Parse with `json.loads`, then `json.loads(repair_json(raw))`; if still invalid, record an error rather than guessing.

The tool-less Agent uses `parallel_tool_calls=None`, existing extra headers, and existing timeout args. `run_pairwise_judging` uses `asyncio.Semaphore(4)`, preserves suite/repeat order, and catches each pair independently.

- [ ] **Step 5: Verify GREEN and commit**

Run: `pytest tests/test_evaluation_judge.py tests/test_skill_evaluator.py -v`

Expected: all pass with no live LLM calls.

```bash
git add openkb/evaluation/judge.py tests/test_evaluation_judge.py
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "feat(evaluation): add blind answer-quality judge"
node .gitnexus/run.cjs analyze
```

### Task 7: Overall release-gate report

**Files:**
- Create: `openkb/evaluation/report.py`
- Create: `tests/test_evaluation_report.py`

**Interfaces:**
- Consumes: `DeterministicComparison` and `JudgeReport`.
- Produces: `ComparisonReport`; `build_comparison_report`; `comparison_to_dict`; `render_comparison_markdown`; `write_comparison_report`.

```python
@dataclass(frozen=True)
class ComparisonReport:
    status: GateStatus
    reasons: tuple[str, ...]
    deterministic: DeterministicComparison
    judge: JudgeReport
    dimension_win_rates: dict[str, float]
```

- [ ] **Step 1: Write failing overall-gate tests**

```python
from openkb.evaluation.report import build_comparison_report, render_comparison_markdown

def test_ties_with_no_retrieval_regression_pass(deterministic_pass, judge_ties):
    report = build_comparison_report(deterministic_pass, judge_ties)
    assert report.status == "pass"
    assert report.dimension_win_rates["correctness"] == 0.5

def test_judge_loss_fails(deterministic_pass, judge_candidate_loses):
    assert build_comparison_report(
        deterministic_pass, judge_candidate_loses
    ).status == "fail"

def test_judge_error_is_inconclusive(deterministic_pass, judge_with_error):
    assert build_comparison_report(
        deterministic_pass, judge_with_error
    ).status == "inconclusive"

def test_markdown_contains_gate_and_cutoff(deterministic_pass, judge_ties):
    text = render_comparison_markdown(
        build_comparison_report(deterministic_pass, judge_ties)
    )
    assert "# OpenKB Quality Comparison" in text
    assert "PASS" in text
    assert "Recall@5" in text
```

Construct `deterministic_pass` with equal baseline/candidate `RunScores`. Construct `judge_ties` with one tie verdict for each case/repeat. Derive `judge_candidate_loses` by replacing correctness with `baseline`; derive `judge_with_error` by adding one error string. These fixtures use the exact dataclasses above.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_evaluation_report.py -v`

Expected: module import fails.

- [ ] **Step 3: Implement aggregation and status precedence**

Score candidate win `1.0`, tie `0.5`, baseline win `0.0`. Every dimension mean must be at least `0.5`.

Status precedence is exact:

1. `inconclusive` if deterministic compatibility is inconclusive, any required observation is missing/errored, or any judge verdict is missing/errored;
2. `fail` if deterministic retrieval fails or any dimension mean is below `0.5`;
3. `pass` otherwise.

Include per-cutoff recalls, per-case regressions, dimension win/tie/loss counts and means, errors, suite/corpus/profile hashes, labels and OpenKB versions.

- [ ] **Step 4: Implement atomic outputs**

```python
def write_comparison_report(output_dir, report):
    json_path = output_dir / "comparison.json"
    markdown_path = output_dir / "comparison.md"
    atomic_write_json(json_path, comparison_to_dict(report), ensure_ascii=False)
    atomic_write_text(markdown_path, render_comparison_markdown(report))
    return json_path, markdown_path
```

- [ ] **Step 5: Verify GREEN and commit**

Run: `pytest tests/test_evaluation_report.py tests/test_evaluation_metrics.py tests/test_evaluation_judge.py -v`

Expected: all pass.

```bash
git add openkb/evaluation/report.py tests/test_evaluation_report.py
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "feat(evaluation): report quality release gates"
node .gitnexus/run.cjs analyze
```

### Task 8: Add the `openkb quality` CLI workflow

**Files:**
- Create: `openkb/evaluation/cli.py`
- Modify: `openkb/cli.py:3808` (append registration after existing definitions)
- Create: `tests/test_quality_cli.py`

**Interfaces:**
- Consumes: R0-A services and injected current-CLI callbacks.
- Produces: `register_quality_commands(root: click.Group, *, find_kb_dir: Callable, setup_llm_key: Callable) -> None`; `quality freeze`, `quality run`, `quality compare`.

- [ ] **Step 1: Run impact analysis before editing the existing CLI symbol**

Run:

```bash
gitnexus impact --repo OpenKB --direction upstream --include-tests --file openkb/cli.py --kind Function cli
```

Expected: inspect direct consumers and processes. Stop and report before editing if risk is HIGH/CRITICAL; otherwise record LOW/MEDIUM in the task handoff.

- [ ] **Step 2: Write failing CLI tests**

```python
from unittest.mock import AsyncMock, patch
from click.testing import CliRunner
from openkb.cli import cli

def test_quality_freeze_writes_suite(kb_dir, tmp_path):
    output = tmp_path / "suite.json"
    result = CliRunner().invoke(cli, [
        "--kb-dir", str(kb_dir), "quality", "freeze",
        "tests/fixtures/evaluation/questions-v1.json",
        "--output", str(output),
    ])
    assert result.exit_code == 0
    assert output.is_file()
    assert "Frozen suite" in result.output

def test_quality_compare_returns_one_for_regression(kb_dir, tmp_path):
    paths = [tmp_path / name for name in ("suite.json", "base.json", "candidate.json")]
    for path in paths:
        path.write_text("{}", encoding="utf-8")
    fake_report = type("Report", (), {"status": "fail"})()
    with patch(
        "openkb.evaluation.cli.compare_runs",
        new=AsyncMock(return_value=fake_report),
    ):
        result = CliRunner().invoke(cli, [
            "--kb-dir", str(kb_dir), "quality", "compare",
            *(str(path) for path in paths),
            "--output-dir", str(tmp_path / "comparison"),
        ])
    assert result.exit_code == 1
```

The CLI fixture copies the checked-in Wiki into `kb_dir/wiki` and creates `.openkb`.

- [ ] **Step 3: Verify RED**

Run: `pytest tests/test_quality_cli.py -v`

Expected: Click reports no command `quality`.

- [ ] **Step 4: Implement dependency-injected commands**

```python
def register_quality_commands(root, *, find_kb_dir, setup_llm_key):
    @root.group(name="quality")
    def quality():
        """Freeze and compare evidence-aware QA quality baselines."""

    @quality.command(name="freeze")
    @click.argument(
        "questions", type=click.Path(exists=True, dir_okay=False, path_type=Path)
    )
    @click.option(
        "--output", type=click.Path(dir_okay=False, path_type=Path), required=True
    )
    @click.pass_context
    def freeze_command(ctx, questions, output):
        kb_dir = _require_kb(ctx, find_kb_dir)
        suite = freeze_suite(kb_dir, load_question_set(questions))
        save_suite(output, suite)
        click.echo(f"Frozen suite: {output} ({suite.corpus.digest})")
```

Add exact contracts:

- `quality freeze QUESTIONS --output SUITE`
- `quality run SUITE --label LABEL --output RUN_JSON [--resume]`
- `quality compare SUITE BASELINE_RUN CANDIDATE_RUN --output-dir DIR [--judge-model MODEL]`

`run` loads the effective KB model/language, calls `setup_llm_key`, creates `BaselineQueryExecutor`, then calls `run_suite(suite, label=label, run_path=output, executor=executor, resume=resume)` through `asyncio.run`. `--resume` requires an existing output file. Exit `0` for complete and `2` for partial.

`compare_runs(suite_path: Path, baseline_path: Path, candidate_path: Path, *, kb_dir: Path, judge_model: str, output_dir: Path) -> ComparisonReport` validates deterministic inputs before starting the judge. The command uses `--judge-model` or the effective KB model, writes both reports, and exits `0=pass`, `1=fail`, `2=inconclusive/invalid`. Use `click.ClickException`; never print headers or evidence contents.

- [ ] **Step 5: Register without adding logic to the grandfathered CLI module**

Append to `openkb/cli.py`:

```python
from openkb.evaluation.cli import register_quality_commands

register_quality_commands(
    cli,
    find_kb_dir=_find_kb_dir,
    setup_llm_key=_setup_llm_key,
)
```

- [ ] **Step 6: Verify GREEN and command help**

Run: `pytest tests/test_quality_cli.py tests/test_cli.py tests/test_query.py -v`

Expected: all pass.

Run: `openkb quality --help`

Expected: lists `freeze`, `run`, and `compare`.

- [ ] **Step 7: Commit**

```bash
git add openkb/evaluation/cli.py openkb/cli.py tests/test_quality_cli.py
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "feat(cli): add quality baseline workflow"
node .gitnexus/run.cjs analyze
```

### Task 9: Offline end-to-end workflow, documentation and verification

**Files:**
- Create: `tests/test_evaluation_workflow.py`
- Modify: `README.md:206` (add “Quality regression gate” after Query & Chat)

**Interfaces:**
- Consumes: public R0-A CLI/services.
- Produces: offline freeze → run → compare acceptance and operator runbook.

- [ ] **Step 1: Write the offline end-to-end test**

```python
class GroundedFakeExecutor:
    def __init__(self, language, corpus_sha256):
        self.corpus_sha256 = corpus_sha256
        self.profile = RunProfile(
            "fake/model", language, 50,
            "settings", "provider", "prompt", "test",
        )

    async def run(self, case, repeat_index):
        hit = RetrievedEvidence(
            EvidenceLocator("page_range", document="alpha", pages=(2,)), 1
        )
        return QueryObservation(
            case.case_id, repeat_index, "success",
            "Alice owns Project Alpha.", (hit,), 1,
        )

def all_tie_judge_report(suite):
    profile = JudgeProfile("fake/judge", "settings", "provider", "prompt")
    verdicts = tuple(
        PairwiseVerdict(
            case.case_id,
            repeat_index,
            {dimension: "tie" for dimension in JUDGE_DIMENSIONS},
            "Answers are equivalent.",
        )
        for case in suite.questions.cases
        for repeat_index in range(suite.questions.repeats)
    )
    return JudgeReport(profile, verdicts, ())

@pytest.mark.asyncio
async def test_freeze_capture_and_self_compare_passes(tmp_path):
    kb_dir = make_fixture_kb(tmp_path)
    questions = load_question_set(
        Path("tests/fixtures/evaluation/questions-v1.json")
    )
    suite = freeze_suite(kb_dir, questions)
    run_path = tmp_path / ".openkb/evaluations/runs/baseline/run.json"
    baseline = await run_suite(
        suite,
        label="baseline",
        run_path=run_path,
        executor=GroundedFakeExecutor(
            suite.questions.language, suite.corpus.digest
        ),
    )
    candidate = replace(baseline, label="candidate")
    deterministic = compare_deterministic(suite, baseline, candidate)
    report = build_comparison_report(
        deterministic, all_tie_judge_report(suite)
    )
    json_path, md_path = write_comparison_report(
        tmp_path / "comparison", report
    )
    assert report.status == "pass"
    assert json_path.is_file()
    assert "PASS" in md_path.read_text(encoding="utf-8")
```

`GroundedFakeExecutor` returns rank-1 document `alpha`, page 2, and the reviewed answer. `all_tie_judge_report` contains one successful verdict per case/repeat and no errors.

- [ ] **Step 2: Verify the offline workflow**

Run: `pytest tests/test_evaluation_workflow.py -v`

Expected: passes without network access.

- [ ] **Step 3: Document the exact operator workflow**

Add this command sequence:

```bash
openkb quality freeze questions.json --output .openkb/evaluations/suites/baseline-v1.json
openkb quality run .openkb/evaluations/suites/baseline-v1.json --label baseline --output .openkb/evaluations/runs/baseline-v1/run.json
openkb quality compare .openkb/evaluations/suites/baseline-v1.json .openkb/evaluations/runs/baseline-v1/run.json .openkb/evaluations/runs/candidate-v1/run.json --output-dir .openkb/evaluations/comparisons/baseline-vs-candidate
```

Include the complete Task 1 JSON shape. Explain that `freeze` binds query-visible files, any such file change invalidates the suite, model settings and repeats must match, and exit codes are stable.

State that the reviewed 69-document production question set is local and not bundled publicly. A live baseline is required before R0-B, but the operator starts it only after reviewing questions and confirming possible LLM cost.

- [ ] **Step 4: Run all focused evaluation tests**

Run:

```bash
pytest tests/test_evaluation_schema.py tests/test_evaluation_fingerprint.py tests/test_evaluation_evidence.py tests/test_evaluation_runner.py tests/test_evaluation_metrics.py tests/test_evaluation_judge.py tests/test_evaluation_report.py tests/test_quality_cli.py tests/test_evaluation_workflow.py -v
```

Expected: all pass with zero network calls.

- [ ] **Step 5: Run repository-wide verification**

Run each command separately and require exit 0:

```bash
pytest
ruff check .
ruff format --check .
mypy openkb
pytest tests/test_file_size.py -v
gitnexus detect-changes --repo OpenKB --scope compare --base-ref main
git diff --check
```

If an unrelated pre-existing failure appears, preserve the output and distinguish it from R0-A; do not claim full success.

- [ ] **Step 6: Commit workflow documentation**

```bash
git add tests/test_evaluation_workflow.py README.md
gitnexus detect-changes --repo OpenKB --scope staged
git diff --cached --check
git commit -m "docs: document the quality regression gate"
node .gitnexus/run.cjs analyze
```

## R0-A Traceability

| Approved requirement | Implemented by |
|---|---|
| Fixed corpus and reviewed questions | Tasks 1–2 |
| Same model profile and at least three repetitions | Tasks 1, 4–5 |
| Preserve existing PageIndex/Wiki query behavior | Tasks 3–4, 8 |
| Recall@K and per-question critical evidence cannot drop | Task 5 |
| Correctness, completeness, citation quality and unsupported assertions | Tasks 6–7 |
| Evidence is openable and errors never count as success | Tasks 3, 6–7 |
| Reproducible, resumable, machine-readable runs | Task 4 |
| Release-blocking pass/fail/inconclusive outputs | Tasks 7–8 |
| Offline tests and full repository verification | Task 9 |

Model timeout retries remain intentionally assigned to R0-C Model Gateway, as specified by the approved implementation decomposition. R0-A records current-query failures and marks incomplete runs; it does not alter production retry semantics.

## Completion Gate

R0-A is complete only when:

- The offline end-to-end workflow passes without network access.
- A suite refuses to run after any query-visible corpus change.
- Existing `openkb query` behavior/tests remain unchanged.
- Every run checkpoints atomically and resumes only failed/missing observations.
- Per-question critical evidence and aggregate Recall@K cannot regress.
- Blind judging covers correctness, completeness, citation quality, and unsupported assertions.
- Missing/incompatible/error results block release as inconclusive.
- JSON and Markdown reports agree; CLI exit codes are stable.
- Repository tests, lint, format, types and module-size gates pass, or pre-existing unrelated failures are explicitly documented.
- Before R0-B, the operator freezes the reviewed local 69-document suite and captures a complete production-model baseline.
