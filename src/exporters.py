"""
Export functionality for converting data to Excel and CSV formats.
"""

import csv
from datetime import datetime
from io import BytesIO, StringIO
import json
import math
import re
from typing import Any, NamedTuple
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
import xlsxwriter

from .models import ExportFormat, HeaderStyle
from .validators import Rows, Sheets, snake_to_pascal_case

EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_COLUMNS = 16_384
EXCEL_MAX_CELL_CHARS = 32_767
EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Excel keeps 15 significant digits, so longer integers (IDs) are written as text.
EXCEL_MAX_EXACT_INT = 10**15
_ILLEGAL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CSV_FORMULA = re.compile(r"^[=+\-@\t\r]")
_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")
# ISO 8601 dates and times, without a UTC offset or with "Z". Other offsets stay text.
_ISO_DATETIME = re.compile(
	r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?Z?)?"
)


class Table(NamedTuple):
	columns: list[str]
	rows: list[list[Any]]  # only the first `limit` rows when previewing
	total_rows: int
	long_cells: int  # text longer than Excel's per-cell limit


def _flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
	"""Flatten nested objects: {"address": {"city": "X"}} -> {"address.city": "X"}."""
	flat: dict[str, Any] = {}
	for key, value in record.items():
		name = f"{prefix}{key}"
		if isinstance(value, dict) and value:
			flat.update(_flatten(value, f"{name}."))
		else:
			flat[name] = value
	return flat


def _cell(value: Any) -> Any:
	"""Turn a JSON value into something a spreadsheet cell can hold."""
	if isinstance(value, str):
		return _ILLEGAL_CHARACTERS.sub("", value)
	if value is None or isinstance(value, bool):
		return value
	if isinstance(value, int):
		return str(value) if abs(value) >= EXCEL_MAX_EXACT_INT else value
	if isinstance(value, float):
		return value if math.isfinite(value) else str(value)
	if isinstance(value, list | tuple | dict):
		return _ILLEGAL_CHARACTERS.sub(
			"", json.dumps(value, ensure_ascii=False, default=str)
		)
	return str(value)


def build_table(
	rows: Rows, headers: HeaderStyle = "readable", limit: int | None = None
) -> Table:
	"""Flatten nested objects into columns and convert the first `limit` rows to cells."""
	flat = [_flatten(row) for row in rows]
	keys = list(dict.fromkeys(key for row in flat for key in row))
	columns = (
		[snake_to_pascal_case(key) for key in keys] if headers == "readable" else keys
	)
	long_cells = sum(
		isinstance(value, str) and len(value) > EXCEL_MAX_CELL_CHARS
		for row in flat
		for value in row.values()
	)
	body = [[_cell(row.get(key)) for key in keys] for row in flat[:limit]]
	return Table(columns, body, len(rows), long_cells)


def build_tables(
	sheets: Sheets,
	format_type: ExportFormat,
	headers: HeaderStyle = "readable",
	limit: int | None = None,
) -> dict[str, Table]:
	if format_type == "csv" and len(sheets) > 1:
		raise HTTPException(
			status_code=400,
			detail=f"CSV holds a single table, but this data has {len(sheets)} sheets. Choose Excel to keep them all.",
		)
	tables = {name: build_table(rows, headers, limit) for name, rows in sheets.items()}
	if format_type == "excel":
		for name, table in tables.items():
			if (
				table.total_rows >= EXCEL_MAX_ROWS
				or len(table.columns) > EXCEL_MAX_COLUMNS
			):
				raise HTTPException(
					status_code=400,
					detail=f"Sheet '{name}' has {table.total_rows:,} rows and {len(table.columns):,} columns; "
					f"Excel allows {EXCEL_MAX_ROWS - 1:,} rows and {EXCEL_MAX_COLUMNS:,} columns. Export as CSV instead.",
				)
	return tables


def _as_datetime(value: Any) -> datetime | None:
	if (
		isinstance(value, str)
		and 10 <= len(value) <= 27
		and _ISO_DATETIME.fullmatch(value)
	):
		try:
			return datetime.fromisoformat(value)
		except ValueError:  # looks like a date but isn't one, e.g. 2024-02-30
			return None
	return None


def _to_excel(tables: dict[str, Table]) -> bytes:
	buffer = BytesIO()
	options = {
		"constant_memory": True,  # rows are flushed as they're written
		"strings_to_formulas": False,  # "=..." stays text, never a live formula
		"strings_to_urls": False,
		"remove_timezone": True,  # "...Z" times are kept in UTC
	}
	with xlsxwriter.Workbook(buffer, options) as workbook:
		header = workbook.add_format(
			{"bold": True, "font_color": "#FFFFFF", "bg_color": "#1F6F43"}
		)
		date_format = workbook.add_format({"num_format": "yyyy-mm-dd"})
		datetime_format = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
		for name, table in tables.items():
			ws = workbook.add_worksheet(name)
			if not table.columns:
				continue
			for index, column in enumerate(table.columns):
				# Size columns from the first 1,000 rows; scanning everything is slow for little gain.
				longest = max(
					(
						len(str(row[index]))
						for row in table.rows[:1000]
						if row[index] is not None
					),
					default=0,
				)
				ws.set_column(index, index, min(60, max(len(column), longest) + 3))
			ws.freeze_panes(1, 0)
			ws.autofilter(0, 0, len(table.rows), len(table.columns) - 1)
			ws.write_row(0, 0, table.columns, header)
			for row_index, row in enumerate(table.rows, start=1):
				for column_index, value in enumerate(row):
					if (when := _as_datetime(value)) is not None:
						cell_format = (
							date_format if len(value) == 10 else datetime_format
						)
						ws.write_datetime(row_index, column_index, when, cell_format)
					elif value is not None:
						ws.write(row_index, column_index, value)
	return buffer.getvalue()


def _csv_safe(value: Any) -> Any:
	"""Prefix text that Excel would run as a formula (OWASP CSV injection guidance)."""
	if (
		isinstance(value, str)
		and _CSV_FORMULA.match(value)
		and not _NUMBER.match(value)
	):
		return f"'{value}"
	return value


def _to_csv(table: Table) -> bytes:
	buffer = StringIO()
	writer = csv.writer(buffer)
	writer.writerow([_csv_safe(column) for column in table.columns])
	writer.writerows([_csv_safe(value) for value in row] for row in table.rows)
	return buffer.getvalue().encode(
		"utf-8-sig"
	)  # the BOM makes Excel read UTF-8 correctly


def _content_disposition(filename: str, extension: str) -> str:
	name = re.sub(r"\.(xlsx|csv)$", "", filename.strip(), flags=re.IGNORECASE)
	name = (
		re.sub(r'[\x00-\x1f\x7f"\\/:*?<>|]+', "_", name).strip(" .")[:100]
		or "exported_data"
	)
	full_name = f"{name}.{extension}"
	ascii_name = re.sub(r"[^\x20-\x7e]", "_", full_name)
	return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(full_name)}"


def export_data(
	sheets: Sheets,
	format_type: ExportFormat,
	filename: str,
	headers: HeaderStyle = "readable",
) -> StreamingResponse:
	"""Export sheets to an Excel workbook or a CSV file."""
	tables = build_tables(sheets, format_type, headers)
	if format_type == "csv":
		content, media_type, extension = (
			_to_csv(next(iter(tables.values()))),
			"text/csv",
			"csv",
		)
	else:
		content, media_type, extension = _to_excel(tables), EXCEL_MEDIA_TYPE, "xlsx"
	# Sent in chunks: Vercel's 4.5 MB response limit doesn't apply to streamed responses.
	chunks = (content[i : i + 1_048_576] for i in range(0, len(content), 1_048_576))
	return StreamingResponse(
		chunks,
		media_type=media_type,
		headers={"Content-Disposition": _content_disposition(filename, extension)},
	)


def preview_data(
	sheets: Sheets,
	format_type: ExportFormat,
	headers: HeaderStyle = "readable",
	limit: int = 20,
) -> dict[str, Any]:
	"""Describe what the export will contain: per sheet, its columns and first rows."""
	tables = build_tables(sheets, format_type, headers, limit)
	warnings = []
	long_cells = sum(table.long_cells for table in tables.values())
	if format_type == "excel" and long_cells:
		warnings.append(
			f"{long_cells:,} cell{'s' if long_cells != 1 else ''} longer than {EXCEL_MAX_CELL_CHARS:,} "
			"characters will be cut to fit Excel. Choose CSV to keep the full text."
		)
	return {
		"sheets": [
			{
				"name": name,
				"columns": table.columns,
				"rows": table.rows,
				"total_rows": table.total_rows,
			}
			for name, table in tables.items()
		],
		"warnings": warnings,
	}
