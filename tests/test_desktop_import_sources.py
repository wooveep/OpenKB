"""Source-selection checks for Desktop batch import."""

from __future__ import annotations

from pathlib import Path

from openkb.desktop_import_sources import inspect_import_sources


def test_source_inspection_expands_directories_deduplicates_and_sorts(tmp_path):
    """A selection has stable TXT candidates and explicit unprocessable entries."""
    sources = tmp_path / "sources"
    nested = sources / "nested"
    nested.mkdir(parents=True)
    (sources / "zeta.txt").write_text("zeta", encoding="utf-8")
    (sources / "alpha.md").write_text("alpha", encoding="utf-8")
    (nested / "beta.txt").write_text("beta", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    inspection = inspect_import_sources(
        (sources / "zeta.txt", sources, empty, tmp_path / "missing.pdf")
    )

    assert [Path(source.path).name for source in inspection.supported] == ["beta.txt", "zeta.txt"]
    assert [(source.name, source.error_code) for source in inspection.unsupported] == [
        ("empty", "import_directory_empty"),
        ("missing.pdf", "import_source_not_found"),
        ("alpha.md", "unsupported_import_format"),
    ]
    assert inspection.as_dict()["supported_extensions"] == [".txt"]
