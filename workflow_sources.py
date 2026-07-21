"""Local, bounded extraction of workflow source documents."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from config import WORKFLOW_TEXT_LIMIT


MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_TEXT_CHARS = WORKFLOW_TEXT_LIMIT
SUPPORTED_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".docx", ".pdf"})


class MaterialError(ValueError):
    """A user-facing problem with a locally selected workflow material."""


@dataclass(frozen=True)
class FileMaterial:
    """Extracted content kept only for the current workflow run."""

    name: str
    text: str

    def source_summary(self) -> dict[str, str]:
        return {"kind": "file", "name": self.name}


def extract_file_materials(paths: Iterable[str | Path]) -> list[FileMaterial]:
    """Read supported local documents with fixed count, size, and text budgets."""
    selected = [Path(path) for path in paths]
    if len(selected) > MAX_FILES:
        raise MaterialError(f"Select at most {MAX_FILES} files for one workflow.")

    materials: list[FileMaterial] = []
    total_chars = 0
    for path in selected:
        if not path.is_file():
            raise MaterialError(f"File is unavailable: {path.name}")
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise MaterialError(f"Unsupported file type: {path.name}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise MaterialError(f"File is too large (maximum {MAX_FILE_BYTES // (1024 * 1024)} MiB): {path.name}")

        text = _extract_text(path, suffix)
        if not text.strip():
            raise MaterialError(f"No extractable text was found in: {path.name}")
        total_chars += len(text)
        if total_chars > MAX_TOTAL_TEXT_CHARS:
            raise MaterialError(
                f"Selected files exceed the {MAX_TOTAL_TEXT_CHARS:,}-character text limit."
            )
        materials.append(FileMaterial(name=path.name, text=text))
    return materials


def _extract_text(path: Path, suffix: str) -> str:
    if suffix in {".txt", ".md", ".markdown"}:
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MaterialError(f"Text file must use UTF-8 encoding: {path.name}") from exc
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    raise MaterialError(f"Unsupported file type: {path.name}")


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency declaration is tested by installation
        raise MaterialError("DOCX support is unavailable; install python-docx and restart.") from exc
    try:
        document = Document(path)
    except Exception as exc:
        raise MaterialError(f"Couldn't read DOCX file: {path.name}") from exc
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency declaration is tested by installation
        raise MaterialError("PDF support is unavailable; install pypdf and restart.") from exc
    try:
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise MaterialError(f"Couldn't extract text from PDF: {path.name}") from exc
