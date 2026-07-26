"""Parsers and profilers for heterogeneous client files.

This module converts raw file bytes into structured facts and Polars DataFrames.
Supported inputs include spreadsheets (CSV, XLSX), PDFs, emails (EML), and
plain-text documents (TXT, MD, DOCX). All functions are pure: they take bytes
and return structured data without side effects.
"""

from __future__ import annotations

import email
import io
from collections import Counter
from typing import Any, cast

import polars as pl
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet


def load_workbook_from_bytes(file_bytes: bytes) -> Workbook:
    """Load an Excel workbook from bytes for read-only inspection."""
    return load_workbook(
        filename=io.BytesIO(file_bytes),
        read_only=True,
        data_only=True,
    )


def get_sheet_names(file_bytes: bytes) -> list[str]:
    """Return the list of sheet names in an Excel workbook."""
    wb = load_workbook_from_bytes(file_bytes)
    names = list(wb.sheetnames)
    wb.close()
    return names


def summarise_sheet(
    file_bytes: bytes,
    *,
    sheet_name: str | None = None,
    max_sample_values: int = 8,
    max_distinct_categorical: int = 15,
    max_cols: int = 52,
) -> dict[str, Any]:
    """Build a smart per-column summary of a spreadsheet sheet."""
    wb = load_workbook_from_bytes(file_bytes)

    if sheet_name:
        if sheet_name not in wb.sheetnames:
            wb.close()
            raise ValueError(
                f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}"
            )
        ws = cast(Worksheet, wb[sheet_name])
    else:
        ws = cast(Worksheet, wb.active)
        sheet_name = ws.title

    total_rows = ws.max_row or 0
    total_cols = ws.max_column or 0
    col_count = min(max_cols, total_cols) if total_cols else max_cols

    all_rows: list[list[Any]] = []
    for worksheet_row in ws.iter_rows(max_col=col_count, values_only=True):
        all_rows.append(list(worksheet_row))

    wb.close()

    if not all_rows:
        return {
            "sheet_name": sheet_name,
            "total_rows": 0,
            "total_cols": total_cols,
            "header_row": 0,
            "columns": [],
        }

    header_row_idx = 0
    for idx, candidate_row in enumerate(all_rows):
        non_empty = [c for c in candidate_row if c is not None]
        if non_empty and all(isinstance(c, str) for c in non_empty):
            header_row_idx = idx
            break
        if non_empty:
            header_row_idx = idx
            break

    headers = (
        all_rows[header_row_idx]
        if header_row_idx < len(all_rows)
        else [None] * col_count
    )
    data_rows = all_rows[header_row_idx + 1 :]

    column_summaries: list[dict[str, Any]] = []
    for col_idx in range(col_count):
        col_letter = get_column_letter(col_idx + 1)
        header_val = headers[col_idx] if col_idx < len(headers) else None

        values = []
        for data_row in data_rows:
            if col_idx < len(data_row) and data_row[col_idx] is not None:
                values.append(data_row[col_idx])

        if not values:
            column_summaries.append(
                {
                    "column_letter": col_letter,
                    "header": str(header_val) if header_val is not None else None,
                    "non_empty_count": 0,
                    "first_values": [],
                    "last_values": [],
                    "dominant_type": None,
                    "type_inconsistencies": None,
                    "distinct_values": None,
                }
            )
            continue

        first_vals = [str(v) for v in values[:max_sample_values]]
        last_vals = (
            [str(v) for v in values[-max_sample_values:]]
            if len(values) > max_sample_values
            else []
        )

        type_counts = Counter(type(v).__name__ for v in values)
        dominant_type = type_counts.most_common(1)[0][0]
        type_inconsistencies = None
        if len(type_counts) > 1:
            minority_types = {
                t: c for t, c in type_counts.items() if t != dominant_type
            }
            type_inconsistencies = (
                f"Mostly {dominant_type} ({type_counts[dominant_type]}/"
                f"{len(values)}) but also: "
                f"{', '.join(f'{t} ({c})' for t, c in minority_types.items())}"
            )

        distinct_raw = {str(v) for v in values}
        distinct_values = None
        if len(distinct_raw) <= max_distinct_categorical:
            distinct_values = sorted(distinct_raw)

        column_summaries.append(
            {
                "column_letter": col_letter,
                "header": str(header_val) if header_val is not None else None,
                "non_empty_count": len(values),
                "first_values": first_vals,
                "last_values": last_vals,
                "dominant_type": dominant_type,
                "type_inconsistencies": type_inconsistencies,
                "distinct_values": distinct_values,
            }
        )

    return {
        "sheet_name": sheet_name,
        "total_rows": total_rows,
        "total_cols": total_cols,
        "header_row": header_row_idx + 1,
        "data_start_row": header_row_idx + 2,
        "columns": column_summaries,
    }


def read_csv_to_polars(file_bytes: bytes, **options: Any) -> pl.DataFrame:
    """Parse CSV bytes into a Polars DataFrame."""
    return pl.read_csv(io.BytesIO(file_bytes), **options)


def read_excel_to_polars(
    file_bytes: bytes,
    sheet_name: str | None = None,
    **options: Any,
) -> pl.DataFrame:
    """Parse Excel bytes into a Polars DataFrame."""
    return cast(
        pl.DataFrame,
        pl.read_excel(
            io.BytesIO(file_bytes),
            sheet_name=sheet_name,
            engine="openpyxl",
            **options,
        ),
    )


def parse_pdf_to_text(file_bytes: bytes) -> dict[str, Any]:
    """Extract text and page references from a PDF."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return {"pages": [], "full_text": "", "error": "pypdf not installed"}

    reader = PdfReader(io.BytesIO(file_bytes))
    pages = []
    full_text_parts = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append({"page": i, "text": text})
        full_text_parts.append(text)
    return {
        "pages": pages,
        "full_text": "\n".join(full_text_parts),
    }


def parse_email_to_dict(file_bytes: bytes) -> dict[str, Any]:
    """Parse an email message into structured headers and body."""
    msg = email.message_from_bytes(file_bytes)
    headers = dict(msg.items())
    body_text_parts = []
    body_html_parts = []
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            filename = part.get_filename()
            if filename:
                attachments.append(filename)
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            if not isinstance(payload, bytes):
                continue
            text = payload.decode("utf-8", errors="ignore")
            if content_type == "text/plain":
                body_text_parts.append(text)
            elif content_type == "text/html":
                body_html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            body_text_parts.append(payload.decode("utf-8", errors="ignore"))

    return {
        "headers": headers,
        "body_text": "\n".join(body_text_parts),
        "body_html": "\n".join(body_html_parts),
        "attachments": attachments,
    }


def parse_text_document(file_bytes: bytes, mime_type: str) -> dict[str, Any]:
    """Parse plain-text, markdown, or Word documents into text."""
    text = file_bytes.decode("utf-8", errors="ignore")
    if "wordprocessingml" in mime_type or file_bytes.startswith(b"PK"):
        try:
            from docx import Document
        except ImportError:  # pragma: no cover
            return {"text": "", "error": "python-docx not installed"}
        doc = Document(io.BytesIO(file_bytes))
        text = "\n".join(p.text for p in doc.paragraphs)
    return {"text": text, "sections": []}


def extract_evidence_chunks(
    parsed_document: dict[str, Any],
    raw_file_id: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> list[dict[str, Any]]:
    """Split a parsed document into searchable evidence chunks."""
    text = ""
    if "full_text" in parsed_document:
        text = parsed_document["full_text"]
    elif "text" in parsed_document:
        text = parsed_document["text"]
    elif "body_text" in parsed_document:
        text = parsed_document["body_text"]

    chunks: list[dict[str, Any]] = []
    start = 0
    idx = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if not chunk.strip():
            break
        chunks.append(
            {
                "raw_file_id": raw_file_id,
                "evidence_type": "text_chunk",
                "content": chunk,
                "page_ref": None,
                "chunk_index": idx,
                "metadata": {},
            }
        )
        start = end - chunk_overlap
        idx += 1
    return chunks


def build_polars_from_mapping_source(
    file_bytes: bytes,
    file_type: str,
    source_table: str,
) -> pl.DataFrame:
    """Load any supported raw file into a Polars DataFrame for a mapping source."""
    if file_type == "csv":
        return read_csv_to_polars(file_bytes)
    if file_type == "xlsx":
        return read_excel_to_polars(file_bytes, sheet_name=source_table or None)
    raise ValueError(f"Cannot build Polars DataFrame from file type '{file_type}'")


def profile_polars_dataframe(
    df: pl.DataFrame,
    source_table: str,
) -> dict[str, Any]:
    """Profile a Polars DataFrame and emit column-level statistics."""
    columns = []
    for name in df.columns:
        series = df[name]
        non_null = series.drop_nulls()
        sample_values = [str(v) for v in non_null.head(5).to_list()]
        distinct = non_null.unique().to_list()
        columns.append(
            {
                "column": name,
                "dtype": str(series.dtype),
                "null_count": int(series.null_count()),
                "non_null_count": len(non_null),
                "unique_count": len(distinct),
                "sample_values": sample_values,
            }
        )
    return {
        "source_table": source_table,
        "row_count": len(df),
        "columns": columns,
    }
