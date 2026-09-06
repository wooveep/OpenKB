# OpenKB Desktop

OpenKB Desktop is a local knowledge workbench for importing documents, asking
grounded questions, reviewing extracted concepts and entities, and keeping a
SQLite-backed knowledge base on your computer.

It is designed to run from an extracted portable package. The native Tauri
shell owns the window and tray, while a bundled Python Engine handles document
parsing, local storage, retrieval, and model calls over a private stdio bridge.
It does not expose a user-facing local HTTP server. The legacy DOC/PPT parser
uses its package-local Tika helper only on loopback and only while it is needed.

## Use the desktop app

1. Extract the platform package and launch the OpenKB application.
2. Create a new knowledge-base folder or open an existing Desktop knowledge
   base.
3. Configure the model and enter its API key in **Settings**. Desktop keeps the
   configuration in the knowledge base’s `.openkb/config.yaml`; saved keys are
   masked when settings are read back. Desktop does not resolve keys from `.env`
   or environment variables. The ignored repository-root `.env` is used only by
   developer live-evaluation commands.
4. Import PDF, Markdown, TXT, DOC/DOCX, XLS/XLSX, and PPT/PPTX files.
5. Ask questions from the **Ask** workspace. Answers show their cited document
   sections and relevant source images.

Imported documents are also compiled into a virtual **Knowledge Navigation
View**. It gives question answering stable routes across document summaries,
entities, concepts, procedures, and source sections without making a second
copy of the knowledge base authoritative. Complex procedural questions can
open a bounded number of related pages and source checkpoints to fill missing
scope, safety, or validation details; narrow lookups stay on their exact route.

From the **Knowledge** workspace, an optional **Portable Wiki Export** can write
a read-only snapshot containing an index, summaries, entities, concepts,
procedures, source pages, and a checksum manifest. OpenKB previews and validates
the snapshot before publishing it; the SQLite knowledge base remains the source
of truth.

Imports are staged and resumable. Provider connection failures may retry within
the bounded connection policy. An established model request waits for an explicit
terminal result or cancellation; retrieval budgets are checked between operations.
Failed documents remain unavailable until manually recovered. The failed-documents
view can reuse verified parsing or explicitly reparse in automatic, fast, or enhanced
mode; reparsing rebuilds all downstream checkpoints from the saved original.

Page synthesis runs independently of optional graph extraction and coalesces
queued document changes. Identical evidence and model contracts can reuse validated
page plans. Cross-document claim comparisons and ambiguous identity matches appear
in **Review** with source excerpts; decisions are tied to the displayed snapshot.

The knowledge base keeps the original imported bytes under `raw/`, its
authoritative state in `.openkb/state.sqlite3`, and generated knowledge pages
under `knowledge-pages/`. Closing the main window hides it to the tray while
background work continues; choose **Quit OpenKB** from the tray to stop it.

## Desktop diagnostics

OpenKB writes separate, structured Shell and Engine Application Logs under
`%LOCALAPPDATA%\OpenKB\logs`. The default level is `WARN`; each terminal import
or model failure includes a safe Failure Context that identifies the failing
stage and distinguishes connection, provider-response, and model-result
problems without copying document content or model payloads into the log.

To change verbosity, copy `openkb.local.example.json` to `openkb.local.json`
beside `OpenKB.exe`, edit it, and restart OpenKB. For example, this keeps the
global default quiet while enabling normal debugging for the import pipeline:

```json
{
  "logging": {
    "level": "WARN",
    "components": {
      "import": "DEBUG",
      "parser": "DEBUG",
      "model": "DEBUG"
    },
    "allow_sensitive_trace": false
  }
}
```

Levels are `TRACE`, `DEBUG`, `INFO`, `WARN`, and `ERROR`. Components are
`shell`, `bridge`, `runtime`, `import`, `parser`, `model`, `page_tree`,
`retrieval`, `knowledge`, `projection`, and `storage`. Unknown fields are
ignored with a stable warning; invalid known values fail safely to all-`WARN`.

`TRACE` is intentionally different: it can retain unredacted prompt, provider
response/reasoning, path, and exception evidence for failed operations. It is
accepted only when `allow_sensitive_trace` is `true` and
`sensitive_trace_expires_at` is a future UTC timestamp no more than 24 hours
away. Active capture is shown by a persistent red Workbench banner and can be
stopped there. Captures live under
`%LOCALAPPDATA%\OpenKB\sensitive-traces`, are never included in a Diagnostic
Bundle, and are automatically bounded by age, count, and size.

For example, a targeted model trace uses the following shape; replace the
expiry placeholder with an actual future UTC value before starting OpenKB:

```json
{
  "logging": {
    "level": "WARN",
    "components": { "model": "TRACE" },
    "allow_sensitive_trace": true,
    "sensitive_trace_expires_at": "<YYYY-MM-DDTHH:MM:SSZ within 24 hours>"
  }
}
```

## Development

The Desktop package is assembled from a Tauri 2 shell, the React/Vite frontend,
and a Python sidecar. A source checkout needs Python 3.12, Node.js, Rust, and
the Windows packaging prerequisites described by the packaging scripts.

```bash
uv sync --extra dev --extra desktop-build
npm --prefix frontend install
npm --prefix frontend run build
uv run pytest -q
uv run ruff check .
uv run mypy openkb
```

On Windows, use `desktop/scripts/New-PortablePackage.ps1` to build the portable
archive and `desktop/scripts/Test-PortablePackage.ps1` to validate its Engine
and bundled parser assets. See
[the clean-machine release checklist](desktop/PORTABLE_ACCEPTANCE.md) for the
final Windows black-box pass.

## Architecture notes

- Document parsing produces a structured `DocumentIR` before evidence, search,
  graph extraction, and knowledge reconciliation run. Completed stages are
  reused when a document is resumed.
- Retrieval remains embedding-free: lexical FTS, document structure, wiki
  knowledge, and evidence-bound local graph candidates are fused before the
  answer model receives source evidence.
- The graph is stored in SQLite and is an optional quality enhancement. A graph
  timeout or failure silently falls back to ordinary document retrieval.
- Old CLI, browser workbench, REST/SSE API, and legacy filesystem knowledge-base
  operation are no longer supported entry points. Legacy knowledge-base
  migration is deliberately a separate future project.

See [the retrieval evaluation guide](docs/desktop-retrieval-evaluation.md) for
the local-graph quality gate and its required evaluation corpus.

For source placement and shared-module rules, see [the code architecture map](docs/architecture.md).
The frontend builds into `frontend/dist/`; the Python package contains Engine code and data.
