# Windows Portable Package acceptance

This checklist is the release acceptance companion to
`New-PortablePackage.ps1` and `Test-PortablePackage.ps1`. The native Windows
CI job builds and exercises the package automatically; perform this short
black-box pass on each supported clean Windows image before publishing a
release.

## Clean-machine matrix

- Windows 10 22H2 x64 and a supported Windows 11 x64 version.
- No preinstalled Python, Java, Node.js, Rust, Office, LibreOffice, or WebView2
  is required. Disconnect the machine from the network before unpacking.
- Unpack the ZIP into a path containing spaces and non-ASCII characters. Verify
  `OpenKB.exe` is the only user-facing entry point.
- The package-local `runtime/pageindex/OpenKBPageIndex.exe` must pass its
  offline self-check and Markdown tree probe without starting a system Python.
  It is an experimental evaluation worker and remains disabled by default.

## Product pass

1. Start `OpenKB.exe`, create a knowledge base, then import TXT, Markdown with
   a relative image, DOCX, XLSX, PPTX, a text PDF, and a scanned PDF. Confirm
   each completed document can be opened and its source images render in the
   reader. The scanned PDF must complete offline using the packaged enhanced
   parser.
2. Import representative legacy `.doc` and `.ppt` files. Confirm that the
   documented compatibility/low-fidelity text result is usable when parsing
   succeeds, and that encrypted, corrupt, or unsupported files are isolated
   with recovery guidance rather than entering question answering.
3. Ask a question that cites an imported document and image. Confirm that the
   answer shows document/section references and unavailable or quarantined
   documents are excluded.
4. Start a multi-document import, close the main window, and confirm the app
   remains in the tray while the task continues. Reopen from the tray; then
   relaunch the executable with a document path while it is already running.
   The existing window must receive the import intent. Restart the app and
   verify the task/document state is recovered.
5. Choose **Quit OpenKB** from the tray. Verify `OpenKB.exe`, `OpenKBEngine.exe`,
   WebView2 children, and any private Tika Java child are gone.
6. With no `openkb.local.json`, trigger a controlled import failure and confirm
   the JSON Lines Shell and Engine logs under `%LOCALAPPDATA%\OpenKB\logs` contain
   one self-contained terminal `WARN` without document content, credentials, raw
   model payloads, or absolute paths. Copy `openkb.local.example.json` to
   `openkb.local.json`, enable `DEBUG` for `import`, `parser`, and `model`, restart,
   and confirm bounded lifecycle/timing records explain the failing stage without
   one-second polling noise.
7. On a disposable test document, enable `TRACE` with
   `allow_sensitive_trace: true` and a UTC expiry less than 24 hours away. Confirm
   the persistent red banner, the failed-operation-only raw evidence under
   `%LOCALAPPDATA%\OpenKB\sensitive-traces`, the confirmation before opening that
   directory, and the **Stop trace** action. Confirm an expired or malformed
   authorization falls back to all-`WARN`, and that a Diagnostic Bundle contains
   only the support-safe Application Log tails.

## Release inventory

Inspect `release-manifest.json` in the ZIP and the adjacent
`*.release.json` next to it. The first records the versioned package inventory,
payload size, and component sizes; the second records the actual compressed ZIP
size and SHA-256. Confirm the package includes the fixed WebView2 runtime,
Python Engine, DeepDoc model bundle, RapidOCR resources, and private Tika/JRE,
the isolated PageIndex onedir worker, fixed evaluation corpus, exact locks, and
MIT license, along with `LICENSE` and `THIRD_PARTY_NOTICES.md`. Manifest schema 3 records the
PageIndex package/source/provider identity and `defaultEnabled: false`.
The package includes `openkb.local.example.json`; the mutable
`openkb.local.json` override is deliberately excluded from the release inventory.

## PageIndex package decision

Compare the deterministic package built from the fixed baseline commit with
the PageIndex candidate on the same Windows host:

```powershell
./desktop/scripts/Measure-PageIndexPortablePackage.ps1 `
  -BaselinePackageDirectory <baseline-package> `
  -CandidatePackageDirectory <candidate-package> `
  -BaselineReleaseSummary <baseline.release.json> `
  -CandidateReleaseSummary <candidate.release.json> `
  -FixedEvaluationReport <pageindex-package-report.json> `
  -OutputPath <pageindex-portable-acceptance.json>
```

The JSON record captures environment identity, ZIP/expanded/component sizes,
Engine cold-start p95 and peak working set, first PageIndex query latency and
peak working set, crash containment, the fixed evaluation gate, and the final
promotion decision. The packaged Engine validates the report schema, fixed
suite and corpus digests, complete case × repetition × seven-variant coverage,
recomputed metrics/gates, generation-reference closure, exact candidate
provider identity, and packaged worker inventory hash. The typed report itself
records the evaluated Available-corpus digest, worker SHA-256, and final
knowledge/derived snapshots; the audit record stores both report and worker
SHA-256 values. A missing, malformed, incomplete, stale,
unrelated, or failed report, or a cold-start p95 delta above one second, records
`not_promoted`; it never silently changes the default.
Run the fixed report against the packaged worker with
`--experimental-pageindex-worker` as documented in
`docs/desktop-retrieval-evaluation.md`.

## Release record

Do not mark a version accepted until the release notes or its GitHub issue
contains this completed record, linked to the emitted ZIP and `.release.json`:

| Environment | Tester/date | Result | Notes |
| --- | --- | --- | --- |
| Clean offline Windows 10 22H2 x64 | Not available / 2026-08-20 | fail | No supplied clean Win10 image or enabled local VM/Sandbox; treated as a missing release gate. |
| Supplied Windows 11 Enterprise LTSC x64 | OpenKB acceptance / 2026-08-20 | package pass; promotion fail | Black-box children used restricted package-only PATH/offline environment. Host itself contains dev runtimes; fixed Retrieval Evaluation failed. |
| Native Windows package CI artifact | Not run / 2026-08-20 | fail | No workflow artifact/run URL was available for this candidate; not inferred from the local native build. |

The issue #63 decision is recorded in
[`acceptance/2026-08-20-pageindex-windows-decision.md`](acceptance/2026-08-20-pageindex-windows-decision.md).
The supplied Windows 11 host passed the native black-box package checks, but
the fixed Retrieval Evaluation gate failed, the clean Windows 10 row was not
available, and no native CI artifact was available. PageIndex was therefore
**not promoted**; the deterministic provider remains the default and PageIndex
remains experimental and disabled by default.
