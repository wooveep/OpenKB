# Third-party notices for the OpenKB Portable Desktop Package

The Portable Desktop Package includes or is built with the following principal
components. Their respective license texts and notices are retained in the
release package or their distributed runtime directories.

- OpenKB — Apache License 2.0 (`LICENSE`).
- Tauri — MIT OR Apache-2.0.
- PyInstaller 6.22.0 — GPL-2.0-or-later with the PyInstaller bootloader
  exception.
- Microsoft Edge WebView2 Fixed Version Runtime 151.0.4129.86 x64 — Microsoft
  software license terms included with the fixed runtime.
- PyMuPDF 1.27.2.3 — AGPL-3.0-or-later, unless covered by a commercial
  PyMuPDF license.
- RapidOCR ONNX Runtime 1.4.4 and its bundled PP-OCRv4 models — Apache-2.0.
- InfiniFlow DeepDoc OCR ONNX models (`det.onnx`, `rec.onnx`, and `ocr.res`)
  at revision `de0e793dc6d744406c96dabd688ccc969f41b443` — Apache-2.0. This
  bundled detection/recognition pair powers the enhanced PDF OCR route;
  OpenKB retains its own page, layout, and table evidence adapter.
- python-tika 3.3.2 and Apache Tika Server 3.3.2 — Apache-2.0. The Tika
  distribution's NOTICE is retained with the packaged server runtime.
- Eclipse Temurin OpenJDK Runtime 17.0.16+8 x64 — GPL-2.0-only with the
  Classpath Exception; its license and notice files are retained with the
  packaged Java runtime.

This package does not include LibreOffice. Legacy binary DOC/PPT compatibility
uses only the private, package-local Tika and Java runtime above.

The Portable Desktop Package includes an isolated, evaluation-only PageIndex
worker but keeps that provider disabled by default. It uses PageIndex 0.2.10 at
verified release commit `ba0ef02d78034704be049894c463dc606acbd0d7`
(MIT), from the official wheel with SHA-256
`23664dd05636d712eb597a7c9c326f4c14d0b3cf412cd3545662f833af641448`.
The worker also pins PyPDF2 3.0.1 (BSD), python-dotenv 1.2.2 (BSD-3-Clause),
and PyYAML 6.0.3 (MIT). Their installed distribution metadata and license
files are retained inside `runtime/pageindex`; the PageIndex MIT text is also
copied to `runtime/pageindex/PageIndex-MIT.txt` for direct inspection.
