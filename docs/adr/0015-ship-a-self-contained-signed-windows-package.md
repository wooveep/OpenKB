---
status: partially superseded by ADR-0020 and ADR-0022
---

# Ship a self-contained and signed portable Windows package

The first public Desktop Workbench ships as a versioned ZIP for Windows 10
22H2 x64 and supported Windows 11 x64, including a fixed WebView2 runtime, the
Python runtime, and document-processing dependencies behind one executable
entry point. Public binaries are Authenticode-signed and updates replace the
fully stopped program directory without touching separately stored Desktop
Knowledge Bases; this accepts a larger measured-and-recorded release package
and manual updates to make first-release startup deterministic without an
installer or external runtime. ADR-0022 adds bundled DeepDoc ONNX models and a
private Java/Tika runtime, so no fixed package-size range is promised before the
Windows release artifact is assembled and verified.
Because Tauri has no portable ZIP target, a Windows-only release step assembles
and verifies the executable, fixed WebView2, frozen Engine, resources, licenses,
and version manifest before creating the archive.
