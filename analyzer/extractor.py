"""
Text extraction from various file formats.

Supported:
  Plaintext  : .txt .md .json .yaml .yml .csv .log  (and any unrecognised extension)
  MS Office  : .docx  .xlsx  .pptx
  OpenDocument: .odt  .ods  .odp
  PDF        : .pdf   (via PyMuPDF)
"""

from __future__ import annotations

from pathlib import Path


def extract_text(file_path: str | Path) -> str:
    """Return all readable text from *file_path* as a single string."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix in {".docx"}:
        return _extract_docx(path)
    if suffix in {".xlsx"}:
        return _extract_xlsx(path)
    if suffix in {".pptx"}:
        return _extract_pptx(path)
    if suffix in {".odt", ".ods", ".odp"}:
        return _extract_odf(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    # Fallback: read as UTF-8 text (plaintext, markdown, json, yaml, csv, log, …)
    return _extract_plaintext(path)


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

def _extract_plaintext(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_docx(path: Path) -> str:
    import docx  # python-docx

    doc = docx.Document(str(path))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text:
            parts.append(para.text)
    # Also grab text inside tables.
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return "\n".join(parts)


def _extract_xlsx(path: Path) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(values_only=True):
            for cell in row:
                if cell is not None:
                    parts.append(str(cell))
    return "\n".join(parts)


def _extract_pptx(path: Path) -> str:
    from pptx import Presentation  # python-pptx

    prs = Presentation(str(path))
    parts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = para.text
                    if text:
                        parts.append(text)
    return "\n".join(parts)


def _extract_odf(path: Path) -> str:
    from odf import text as odf_text
    from odf.opendocument import load as odf_load
    from odf.element import Element

    doc = odf_load(str(path))
    parts: list[str] = []

    def _walk(node: Element) -> None:
        if node.nodeType == node.TEXT_NODE:
            v = node.data
            if v:
                parts.append(v)
        for child in node.childNodes:
            _walk(child)

    _walk(doc.body)
    return "\n".join(parts)


def _extract_pdf(path: Path) -> str:
    import fitz  # PyMuPDF

    parts: list[str] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            page_text = page.get_text()
            if page_text:
                parts.append(page_text)
    return "\n".join(parts)
