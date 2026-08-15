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
