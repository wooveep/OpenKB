# OpenKB Desktop

OpenKB Desktop is a local knowledge workbench for importing documents, asking
grounded questions, reviewing extracted concepts and entities, and keeping a
SQLite-backed knowledge base on your computer.

It is designed to run from an extracted portable package. The native Tauri
shell owns the window and tray, while a bundled Python Engine handles document
parsing, local storage, retrieval, and model calls over a private stdio bridge.
It does not start a local HTTP server.

## Use the desktop app

1. Extract the platform package and launch the OpenKB application.
2. Create a new knowledge-base folder or open an existing Desktop knowledge
   base.
3. Configure a model and its environment-variable credential reference in
   **Settings**. The secret remains in the environment or the KB-local `.env`
   file; it is never stored in the workbench database or sent to the UI.
4. Import PDF, Markdown, TXT, DOC/DOCX, XLS/XLSX, and PPT/PPTX files.
5. Ask questions from the **Ask** workspace. Answers show their cited document
   sections and relevant source images.

Imports are staged and resumable. A model-analysis timeout retries with a
longer request timeout; configuration, authentication, and format errors are
isolated immediately. Failed documents remain unavailable to question answering
until they are manually resumed from the failed-documents view.

The knowledge base keeps the original imported bytes under `raw/`, its
authoritative state in `.openkb/state.sqlite3`, and generated knowledge pages
under `knowledge-pages/`. Closing the main window hides it to the tray while
background work continues; choose **Quit OpenKB** from the tray to stop it.

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
and bundled parser assets.

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
