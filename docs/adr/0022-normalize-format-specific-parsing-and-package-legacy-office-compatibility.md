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
