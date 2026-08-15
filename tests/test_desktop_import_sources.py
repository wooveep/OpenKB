"""Source-selection checks for Desktop batch import."""

from __future__ import annotations

from pathlib import Path

from openkb.desktop_import_sources import inspect_import_sources


def test_source_inspection_expands_directories_deduplicates_and_sorts(tmp_path):
    """A selection has stable current-parser candidates and explicit rejections."""
    sources = tmp_path / "sources"
    nested = sources / "nested"
    nested.mkdir(parents=True)
    (sources / "zeta.txt").write_text("zeta", encoding="utf-8")
    (sources / "alpha.md").write_text("alpha", encoding="utf-8")
    (sources / "archive.doc").write_bytes(b"legacy document")
    (sources / "ledger.xlsx").write_bytes(b"workbook")
    (sources / "legacy.xls").write_bytes(b"legacy workbook")
    (sources / "slides.ppt").write_bytes(b"legacy presentation")
    (sources / "briefing.pptx").write_bytes(b"presentation")
    (sources / "report.pdf").write_bytes(b"pdf")
    (nested / "beta.txt").write_text("beta", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    inspection = inspect_import_sources(
        (sources / "zeta.txt", sources, empty, tmp_path / "missing.pdf")
    )

    assert [Path(source.path).name for source in inspection.supported] == [
        "alpha.md",
        "archive.doc",
        "briefing.pptx",
        "ledger.xlsx",
        "legacy.xls",
        "beta.txt",
        "report.pdf",
        "slides.ppt",
        "zeta.txt",
    ]
    assert [(source.name, source.error_code) for source in inspection.unsupported] == [
        ("empty", "import_directory_empty"),
        ("missing.pdf", "import_source_not_found"),
    ]
    assert inspection.as_dict()["supported_extensions"] == [
        ".txt",
        ".md",
        ".markdown",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".pdf",
    ]
