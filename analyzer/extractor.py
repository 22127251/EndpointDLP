"""
Text extraction from various file formats.

Plain-text extraction (extract_text)
-------------------------------------
Supported:
  Plaintext   : .txt .md .json .yaml .yml .csv .log  (and any unrecognised extension)
  MS Office   : .docx  .xlsx  .pptx
  OpenDocument: .odt  .ods  .odp
  PDF         : .pdf   (via PyMuPDF)

Tabular extraction (extract_tabular)
--------------------------------------
Returns a TabularData object with per-column structure so that the analysis
engine can use column-header context matching instead of character proximity.

Supported:
  .csv / .tsv          — first row assumed to be column headers
  .xlsx (Excel)        — first row of each sheet assumed to be headers
  .ods (OpenDocument)  — first row of each table assumed to be headers
  .docx                — embedded tables (first row = headers) + body paragraphs
  .odt                 — embedded tables (first row = headers) + body text
"""

from __future__ import annotations

import csv as _csv
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Tabular data types
# ---------------------------------------------------------------------------

@dataclass
class ColumnBlock:
    header: str
    values: list[str]       # one entry per data row; empty cells → ""
    sheet: str | None       # None for single-sheet formats (CSV/TSV)


@dataclass
class TabularData:
    columns: list[ColumnBlock]


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

_TABULAR_SUFFIXES = {".tsv", ".xlsx", ".ods", ".docx", ".odt"}


def is_tabular(file_path: str | Path) -> bool:
    """Return True if the file should be processed with extract_tabular()."""
    return Path(file_path).suffix.lower() in _TABULAR_SUFFIXES


# ---------------------------------------------------------------------------
# Plain-text extraction
# ---------------------------------------------------------------------------

def extract_text(file_path: str | Path) -> str:
    """Return all readable text from *file_path* as a single string."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".xlsx":
        return _extract_xlsx(path)
    if suffix == ".pptx":
        return _extract_pptx(path)
    if suffix in {".odt", ".ods", ".odp"}:
        return _extract_odf(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    return _extract_plaintext(path)


def _extract_plaintext(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_docx(path: Path) -> str:
    import docx

    doc = docx.Document(str(path))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text:
            parts.append(para.text)
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
    from pptx import Presentation

    prs = Presentation(str(path))
    parts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    if para.text:
                        parts.append(para.text)
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
    import fitz

    parts: list[str] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            page_text = page.get_text()
            if page_text:
                parts.append(page_text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tabular extraction
# ---------------------------------------------------------------------------

def extract_tabular(file_path: str | Path) -> TabularData:
    """Extract column-structured data from tabular and document files."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return _extract_csv_tabular(path, delimiter=",")
    if suffix == ".tsv":
        return _extract_csv_tabular(path, delimiter="\t")
    if suffix == ".xlsx":
        return _extract_xlsx_tabular(path)
    if suffix == ".ods":
        return _extract_ods_tabular(path)
    if suffix == ".docx":
        return _extract_docx_tabular(path)
    if suffix == ".odt":
        return _extract_odt_tabular(path)
    # Fallback: treat as single plaintext column
    text = extract_text(path)
    return TabularData(columns=[ColumnBlock(header="", values=text.splitlines(), sheet=None)])


# ---- CSV / TSV ----

def _extract_csv_tabular(path: Path, delimiter: str = ",") -> TabularData:
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        reader = _csv.reader(f, delimiter=delimiter)
        rows = list(reader)

    if not rows:
        return TabularData(columns=[])

    headers = [h.strip() for h in rows[0]]
    data_rows = rows[1:]

    # Fallback: if every non-empty header cell is numeric, treat file as headerless
    non_empty = [h for h in headers if h]
    if non_empty and all(_is_numeric_string(h) for h in non_empty):
        headers = [f"Column {i + 1}" for i in range(len(headers))]
        data_rows = rows

    columns: list[ColumnBlock] = []
    for col_idx, header in enumerate(headers):
        values = [
            row[col_idx].strip() if col_idx < len(row) else ""
            for row in data_rows
        ]
        columns.append(ColumnBlock(header=header, values=values, sheet=None))

    return TabularData(columns=columns)


# ---- Excel (.xlsx) ----

def _extract_xlsx_tabular(path: Path) -> TabularData:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    columns: list[ColumnBlock] = []

    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue

        raw_headers = [str(h) if h is not None else "" for h in rows[0]]
        data_rows = rows[1:]

        non_empty = [h for h in raw_headers if h.strip()]
        if non_empty and all(_is_numeric_string(h) for h in non_empty):
            raw_headers = [f"Column {i + 1}" for i in range(len(raw_headers))]
            data_rows = rows

        for col_idx, header in enumerate(raw_headers):
            values = [
                str(row[col_idx]) if col_idx < len(row) and row[col_idx] is not None else ""
                for row in data_rows
            ]
            columns.append(ColumnBlock(header=header.strip(), values=values, sheet=sheet.title))

    return TabularData(columns=columns)


# ---- ODS (OpenDocument Spreadsheet) ----

def _extract_ods_tabular(path: Path) -> TabularData:
    from odf.opendocument import load as odf_load
    from odf.table import Table, TableRow, TableCell
    from odf import text as odf_text

    doc = odf_load(str(path))
    columns: list[ColumnBlock] = []

    def _cell_text(cell) -> str:
        texts: list[str] = []
        for p in cell.getElementsByType(odf_text.P):
            texts.append(str(p))
        return " ".join(texts).strip()

    for table in doc.spreadsheet.getElementsByType(Table):
        sheet_name: str = table.getAttribute("name") or ""
        table_rows = table.getElementsByType(TableRow)
        if not table_rows:
            continue

        header_cells = table_rows[0].getElementsByType(TableCell)
        raw_headers = [_cell_text(c) for c in header_cells]
        data_table_rows = table_rows[1:]

        non_empty = [h for h in raw_headers if h]
        if non_empty and all(_is_numeric_string(h) for h in non_empty):
            raw_headers = [f"Column {i + 1}" for i in range(len(raw_headers))]
            data_table_rows = table_rows

        for col_idx, header in enumerate(raw_headers):
            values: list[str] = []
            for tr in data_table_rows:
                cells = tr.getElementsByType(TableCell)
                if col_idx < len(cells):
                    values.append(_cell_text(cells[col_idx]))
                else:
                    values.append("")
            columns.append(ColumnBlock(header=header, values=values, sheet=sheet_name))

    return TabularData(columns=columns)


# ---- DOCX (Word document — tables + body paragraphs) ----

def _extract_docx_tabular(path: Path) -> TabularData:
    import docx

    doc = docx.Document(str(path))
    columns: list[ColumnBlock] = []

    # Embedded tables
    for table_idx, table in enumerate(doc.tables):
        if not table.rows:
            continue
        sheet_name = f"Table {table_idx + 1}"
        raw_headers = [cell.text.strip() for cell in table.rows[0].cells]

        for col_idx, header in enumerate(raw_headers):
            values: list[str] = []
            for row in table.rows[1:]:
                if col_idx < len(row.cells):
                    values.append(row.cells[col_idx].text.strip())
                else:
                    values.append("")
            columns.append(ColumnBlock(header=header, values=values, sheet=sheet_name))

    # Body paragraphs as a flat (headerless) column for completeness
    para_texts = [p.text for p in doc.paragraphs if p.text.strip()]
    if para_texts:
        columns.append(ColumnBlock(header="", values=para_texts, sheet=None))

    return TabularData(columns=columns)


# ---- ODT (OpenDocument Text — tables + body text) ----

def _extract_odt_tabular(path: Path) -> TabularData:
    from odf.opendocument import load as odf_load
    from odf.table import Table, TableRow, TableCell
    from odf import text as odf_text

    doc = odf_load(str(path))
    columns: list[ColumnBlock] = []

    def _cell_text(cell) -> str:
        texts: list[str] = []
        for p in cell.getElementsByType(odf_text.P):
            texts.append(str(p))
        return " ".join(texts).strip()

    for table_idx, table in enumerate(doc.body.getElementsByType(Table)):
        sheet_name = f"Table {table_idx + 1}"
        table_rows = table.getElementsByType(TableRow)
        if not table_rows:
            continue

        header_cells = table_rows[0].getElementsByType(TableCell)
        raw_headers = [_cell_text(c) for c in header_cells]

        for col_idx, header in enumerate(raw_headers):
            values: list[str] = []
            for tr in table_rows[1:]:
                cells = tr.getElementsByType(TableCell)
                if col_idx < len(cells):
                    values.append(_cell_text(cells[col_idx]))
                else:
                    values.append("")
            columns.append(ColumnBlock(header=header, values=values, sheet=sheet_name))

    # Body paragraphs (outside tables)
    body_texts: list[str] = []
    for p in doc.body.getElementsByType(odf_text.P):
        t = str(p).strip()
        if t:
            body_texts.append(t)
    if body_texts:
        columns.append(ColumnBlock(header="", values=body_texts, sheet=None))

    return TabularData(columns=columns)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _is_numeric_string(s: str) -> bool:
    return s.strip().lstrip("-").replace(".", "", 1).isdigit()
