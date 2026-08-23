# Normalize format-specific parsing and package legacy Office compatibility

Every imported Raw Asset passes through a format-specific Parser Adapter that
emits validated Document IR before evidence chunking or Markdown materializing.
TXT and Markdown use direct parsers; DOCX, XLS/XLSX, and PPTX use dedicated
Python libraries; PDF uses a PyMuPDF fast path and a bundled CPU DeepDoc ONNX
OCR/layout/table path. Binary DOC and PPT are explicitly low-fidelity
compatibility formats read through packaged python-tika, a private Tika Server
JAR, and a bundled Java runtime. The Portable Desktop Package contains all
models, wheels, JARs, and runtimes and never downloads them on first use. It
does not contain LibreOffice or the RAGFlow service stack. Legacy DOC/PPT only
promise text and metadata; empty or invalid output quarantines the document and
recommends conversion to DOCX/PPTX.

Desktop startup performs only a Parser Readiness Check over packaged assets and
runtimes. It does not instantiate OCR, DeepDoc, or Java. Once import preflight
identifies binary DOC/PPT, Tika may initialize in parallel with Raw Asset
copying and hashing. PDF first performs the PyMuPDF text probe and loads the
OCR/DeepDoc runtime only when the enhanced route is selected. Heavy runtimes
are reused for the life of the Engine and released on exit rather than unloaded
between documents.

PDF selects enhanced parsing when native text is empty or garbled or at least
half of its pages have low text density. Manual recovery may force fast or
enhanced parsing. DOCX and PPTX continue to use their direct structured parsers
and retain embedded Source Images; they OCR those images only during enhanced
recovery when direct document text is insufficient, never for every image by
default.

Every parser result passes the DocumentIR Usability Gate before evidence or
Knowledge Analysis. The gate checks usable text quantity and quality, source
locators, and structural integrity. An insufficient fast result escalates to an
available enhanced route; an insufficient final result stops before any Model
Call and requires manual recovery. Parser Runtime State is reported separately
as resources ready, not loaded, initializing, ready, or unavailable with a
stable diagnostic code, so parser initialization cannot appear as model waiting
or timeout.
