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

## Release inventory

Inspect `release-manifest.json` in the ZIP and the adjacent
`*.release.json` next to it. The first records the versioned package inventory,
payload size, and component sizes; the second records the actual compressed ZIP
size and SHA-256. Confirm the package includes the fixed WebView2 runtime,
Python Engine, DeepDoc model bundle, RapidOCR resources, and private Tika/JRE,
along with `LICENSE` and `THIRD_PARTY_NOTICES.md`.

## Release record

Do not mark a version accepted until the release notes or its GitHub issue
contains this completed record, linked to the emitted ZIP and `.release.json`:

| Environment | Tester/date | Result | Notes |
| --- | --- | --- | --- |
| Clean offline Windows 10 22H2 x64 |  | pass/fail |  |
| Clean offline supported Windows 11 x64 |  | pass/fail |  |
| Native Windows package CI artifact | GitHub Actions | pass/fail | workflow run URL |
