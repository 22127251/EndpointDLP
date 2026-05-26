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

_TABULAR_SUFFIXES = {".tsv", ".xlsx", ".ods", ".docx", ".odt", ".pdf"}


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
    if suffix == ".pdf":
        return _extract_pdf_tabular(path)
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

    # Collect paragraph object IDs that already belong to table cells
    table_para_ids: set[int] = set()
    for tbl in doc.body.getElementsByType(Table):
        for tr in tbl.getElementsByType(TableRow):
            for cell in tr.getElementsByType(TableCell):
                for p in cell.getElementsByType(odf_text.P):
                    table_para_ids.add(id(p))

    # Body paragraphs (outside tables)
    body_texts: list[str] = []
    for p in doc.body.getElementsByType(odf_text.P):
        if id(p) not in table_para_ids:
            t = str(p).strip()
            if t:
                body_texts.append(t)
    if body_texts:
        columns.append(ColumnBlock(header="", values=body_texts, sheet=None))

    return TabularData(columns=columns)


# ---- PDF (PyMuPDF — tables + body text) ----

def _extract_pdf_tabular(path: Path) -> TabularData:
    import fitz

    columns: list[ColumnBlock] = []
    body_texts: list[str] = []
    # Indices into `columns` and column count of the most recently completed
    # multi-row table.  Kept outside the page loop so they survive page turns
    # and can be used to merge 1-row stubs that PyMuPDF creates when a table
    # row wraps across a page boundary.
    last_col_indices: list[int] = []
    last_col_count: int = 0

    with fitz.open(str(path)) as doc:
        for page_num, page in enumerate(doc):
            finder = page.find_tables()
            table_bboxes = [t.bbox for t in finder.tables]

            for table_idx, table in enumerate(finder.tables):
                rows = table.extract()
                if not rows:
                    continue

                if len(rows) == 1:
                    # PyMuPDF splits a page-spanning table into a normal table on
                    # page N and a 1-row "stub" table on page N+1.  The normal
                    # code path would treat that single row as a header with no
                    # data rows, silently discarding every PII value in it.
                    # Instead, merge the stub back into the previous table.
                    stub_row = rows[0]
                    if last_col_indices and len(stub_row) == last_col_count:
                        non_empty = sum(
                            1 for c in stub_row if c is not None and str(c).strip()
                        )
                        if non_empty >= (last_col_count + 1) // 2:
                            # Most cells are populated → the full last row of the
                            # previous table overflowed to this page.  Append each
                            # cell as a new data value in its matching column.
                            for ci, block_idx in enumerate(last_col_indices):
                                cell = stub_row[ci] if ci < len(stub_row) else None
                                columns[block_idx].values.append(
                                    str(cell) if cell is not None else ""
                                )
                        else:
                            # Only a minority of cells have content → just the tail
                            # of one or more cell values overflowed.  Concatenate
                            # each non-empty stub cell onto the last value of the
                            # matching column so the regex sees the complete number
                            # (e.g. "4111 5555 6666" + "\n7777" → full 16-digit VISA).
                            for ci, block_idx in enumerate(last_col_indices):
                                cell = stub_row[ci] if ci < len(stub_row) else None
                                cell_str = str(cell).strip() if cell is not None else ""
                                if cell_str:
                                    if columns[block_idx].values:
                                        columns[block_idx].values[-1] += "\n" + cell_str
                                    else:
                                        columns[block_idx].values.append(cell_str)
                    continue  # stub handled; skip normal header/data processing

                sheet = f"Page {page_num + 1} Table {table_idx + 1}"
                raw_headers = [str(c) if c is not None else "" for c in rows[0]]
                start_idx = len(columns)
                for col_idx, header in enumerate(raw_headers):
                    values: list[str] = []
                    for row in rows[1:]:
                        cell = row[col_idx] if col_idx < len(row) else None
                        values.append(str(cell) if cell is not None else "")
                    columns.append(ColumnBlock(header=header, values=values, sheet=sheet))
                # Record this table's column range so the next stub can find it.
                last_col_indices = list(range(start_idx, len(columns)))
                last_col_count = len(raw_headers)

            # Collect body text at word granularity instead of block granularity.
            # page.get_text("blocks") can merge a paragraph with an adjacent table
            # into one oversized block; filtering at block level then discards the
            # paragraph.  Individual words have tight bboxes so the overlap check
            # correctly distinguishes table words from paragraph words even when
            # PyMuPDF assigned them the same block_no.
            #
            # word_data: (block_no, line_no) → [(x0, word_text)]
            #   block_no – PyMuPDF block index on this page
            #   line_no  – top-to-bottom line counter within that block
            #   x0       – left edge of the word (used for left-to-right ordering)
            word_data: dict[tuple[int, int], list[tuple[float, str]]] = {}
            for word_info in page.get_text("words"):
                wx0, wy0, wx1, wy1, word_text, block_no, line_no, _ = word_info
                in_table = any(
                    not (wx1 <= tb[0] or tb[2] <= wx0 or wy1 <= tb[1] or tb[3] <= wy0)
                    for tb in table_bboxes
                )
                if not in_table:
                    word_data.setdefault((block_no, line_no), []).append((wx0, word_text))

            block_lines: dict[int, list[tuple[int, str]]] = {}
            for (block_no, line_no), words in word_data.items():
                words.sort(key=lambda w: w[0])  # left-to-right within a line
                block_lines.setdefault(block_no, []).append(
                    (line_no, " ".join(w[1] for w in words))
                )

            # Each block becomes one body_texts entry so that context words and
            # PII values in the same paragraph stay in the same "cell" for the
            # engine's inline-context matching.
            for block_no in sorted(block_lines.keys()):
                lines = sorted(block_lines[block_no], key=lambda x: x[0])
                block_text = "\n".join(line for _, line in lines).strip()
                if block_text:
                    body_texts.append(block_text)

    if body_texts:
        columns.append(ColumnBlock(header="", values=body_texts, sheet=None))

    return TabularData(columns=columns)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _is_numeric_string(s: str) -> bool:
    return s.strip().lstrip("-").replace(".", "", 1).isdigit()
