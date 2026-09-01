"""Independent raw-asset structure expectations for source-integrity audits."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path


def expected_structure_kinds(source_format: str, raw_path: Path) -> set[str]:
    """Return only structures that can be recognized conservatively in the raw asset."""
    normalized = source_format.casefold()
    if normalized in {"markdown", "md"}:
        return _markdown_expectations(raw_path)
    if normalized == "docx":
        return _docx_expectations(raw_path)
    if normalized == "xlsx":
        return _office_zip_expectations(raw_path, family="xlsx")
    if normalized == "pptx":
        return _office_zip_expectations(raw_path, family="pptx")
    if normalized == "pdf":
        return _pdf_expectations(raw_path)
    return set()


def _markdown_expectations(raw_path: Path) -> set[str]:
    try:
        content = raw_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    expected: set[str] = set()
    if re.search(r"(?m)^\s{0,3}#{1,6}\s+\S", content):
        expected.add("heading")
    if re.search(r"(?m)^\s{0,3}(?:```|~~~)", content):
        expected.add("code")
    if re.search(r"(?m)^\s*\|?.+\|.+\n\s*\|?\s*:?-{3,}", content):
        expected.add("table")
    return expected


def _docx_expectations(raw_path: Path) -> set[str]:
    try:
        with zipfile.ZipFile(raw_path) as archive:
            document = archive.read("word/document.xml").decode("utf-8", errors="ignore")
            styles = (
                archive.read("word/styles.xml").decode("utf-8", errors="ignore")
                if "word/styles.xml" in archive.namelist()
                else ""
            )
    except (OSError, KeyError, zipfile.BadZipFile):
        return set()
    expected = {"table"} if "<w:tbl" in document else set()
    used_styles = set(re.findall(r'<w:pStyle[^>]+w:val="([^"]+)"', document))
    heading_styles, code_styles = _docx_style_roles(styles)
    if used_styles & heading_styles or any(
        style.casefold().startswith("heading") for style in used_styles
    ):
        expected.add("heading")
    if used_styles & code_styles or any("code" in style.casefold() for style in used_styles):
        expected.add("code")
    return expected


def _docx_style_roles(styles: str) -> tuple[set[str], set[str]]:
    headings: set[str] = set()
    code: set[str] = set()
    for match in re.finditer(
        r'<w:style[^>]+w:styleId="([^"]+)"[^>]*>.*?'
        r'<w:name[^>]+w:val="([^"]+)"',
        styles,
        re.DOTALL,
    ):
        style_id, name = match.group(1), match.group(2).casefold()
        if name.startswith("heading"):
            headings.add(style_id)
        if "code" in name or "preformatted" in name:
            code.add(style_id)
    return headings, code


def _office_zip_expectations(raw_path: Path, *, family: str) -> set[str]:
    try:
        with zipfile.ZipFile(raw_path) as archive:
            names = archive.namelist()
            if family == "xlsx":
                return (
                    {"heading", "table"}
                    if any(name.startswith("xl/worksheets/sheet") for name in names)
                    else set()
                )
            slides = [name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
            expected = {"heading"} if slides else set()
            if any(b"<a:tbl" in archive.read(name) for name in slides):
                expected.add("table")
            return expected
    except (OSError, KeyError, zipfile.BadZipFile):
        return set()


def _pdf_expectations(raw_path: Path) -> set[str]:
    try:
        import pymupdf

        with pymupdf.open(raw_path) as document:
            expected = {"heading"} if document.page_count else set()
            if any(page.find_tables().tables for page in document):
                expected.add("table")
            return expected
    except (OSError, RuntimeError, ValueError):
        return set()
