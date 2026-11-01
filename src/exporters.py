"""
Export functionality for converting data to Excel and CSV formats.

Files are built by generators that pause every CHECKPOINT_ROWS rows. Plain exports run
them straight through; the progress stream reports between pauses, and closing it early
stops the work.
"""

import base64
from collections.abc import Generator, Iterator
import csv
from datetime import datetime
from io import BytesIO, StringIO
import json
import math
import re
import tempfile
import time
from typing import Any, NamedTuple
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
import xlsxwriter

from .models import ExportFormat, HeaderStyle
from .validators import Rows, Sheets, snake_to_pascal_case, validate_and_parse_data

EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_COLUMNS = 16_384
EXCEL_MAX_CELL_CHARS = 32_767
EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Excel keeps 15 significant digits, so longer integers (IDs) are written as text.
EXCEL_MAX_EXACT_INT = 10**15
EXCEL_OPTIONS = {
	"constant_memory": True,  # rows are flushed as they're written
	"strings_to_formulas": False,  # "=..." stays text, never a live formula
	"strings_to_urls": False,
	"remove_timezone": True,  # "...Z" times are kept in UTC
}
CHECKPOINT_ROWS = 500
PROGRESS_INTERVAL = 0.1  # seconds between progress events
FILE_CHUNK_BYTES = 192 * 1024  # 256 KiB once base64-encoded

# Progress counts work, not rows: writing a row costs about one unit per value, plus one
# per this many characters of text (measured).
CHARS_PER_UNIT = {"excel": 200, "csv": 25}
# Step costs vary a lot with the data (text-heavy CSV writes 16x slower than most), so
# jobs of 10 x PROBE_ROWS rows or more time the real steps on a sample spread through it.
PROBE_ROWS = 500
# Typical costs for smaller jobs: per row to flatten, per unit to write and zip.
STEP_COST = {
	"excel": {"flatten": 1.0, "write": 2.0, "finish": 0.16},
	"csv": {"flatten": 1.0, "write": 0.15, "finish": 0.003},
}
TYPICAL_UNITS_PER_ROW = 10.0
# Turning the written CSV into bytes takes about this share of writing it.
CSV_ENCODE_SHARE = 0.02
_ILLEGAL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CSV_FORMULA = re.compile(r"^[=+\-@\t\r]")
_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")
# ISO 8601 dates and times, without a UTC offset or with "Z". Other offsets stay text.
_ISO_DATETIME = re.compile(
	r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?Z?)?"
)

type Steps[T] = Generator[None, None, T]


class Table(NamedTuple):
	columns: list[str]
	keys: list[str]
	rows: list[dict[str, Any]]  # flattened rows; cells are converted while writing
	blocks: list[float]  # work units in each run of CHECKPOINT_ROWS rows
	long_cells: int  # text longer than Excel's per-cell limit

	def cells(self, row: dict[str, Any]) -> list[Any]:
		return [_cell(row.get(key)) for key in self.keys]


class ExportFile(NamedTuple):
	content: bytes
	media_type: str
	extension: str


class Progress:
	"""Estimates how much of a conversion is done, by time.

	A step's remaining time is its remaining work (rows to flatten, units to write and
	zip) times its cost: measured live for the current step (leaving out its first,
	warm-up block), otherwise the sampled cost scaled by how the job's real speed so far
	compares with the sampled costs.
	"""

	def __init__(
		self, total_rows: int, cost: dict[str, float], units_per_row: float
	) -> None:
		self.total_rows = total_rows
		self.cost = cost
		# Estimated until every row is flattened.
		self.units = total_rows * units_per_row
		self.done = {"flatten": 0.0, "write": 0.0, "finish": 0.0}
		self.spent = {"flatten": 0.0, "write": 0.0, "finish": 0.0}  # seconds
		self.blocks = {"flatten": 0, "write": 0, "finish": 0}
		self.warm_up: dict[str, tuple[float, float]] = {}  # each step's first block
		self.step = "flatten"
		self._checkpoint = time.perf_counter()
		self._reported = 0.0

	def add(self, step: str, amount: float) -> None:
		"""Record finished work. The time since the last call was spent on it."""
		now = time.perf_counter()
		self.spent[step] += now - self._checkpoint
		self._checkpoint = now
		self.done[step] += amount
		self.blocks[step] += 1
		self.warm_up.setdefault(step, (self.spent[step], self.done[step]))
		self.step = step

	def _live_rate(self, step: str) -> float | None:
		spent, done = self.spent[step], self.done[step]
		warm_spent, warm_done = self.warm_up.get(step, (0.0, 0.0))
		if done > warm_done:
			return (spent - warm_spent) / (done - warm_done)
		return spent / done if done else None

	@property
	def settled(self) -> bool:
		"""Whether the estimate is steady enough to show as time left: once the writing,
		which takes most of the time, has two blocks timed past its warm-up."""
		return self.blocks["write"] >= 3

	def seconds_left(self) -> float:
		predicted = sum(self.done[step] * self.cost[step] for step in self.done)
		drift = sum(self.spent.values()) / predicted if predicted else 1.0
		work = {"flatten": self.total_rows, "write": self.units, "finish": self.units}
		left = 0.0
		for step, total in work.items():
			rate = self._live_rate(step) if step == self.step else None
			if rate is None:
				rate = self.cost[step] * drift
			left += max(0.0, total - self.done[step]) * rate
		return left

	@property
	def fraction(self) -> float:
		spent = sum(self.spent.values())
		total = spent + self.seconds_left()
		current = min(1.0, spent / total) if total else 1.0
		self._reported = max(self._reported, current)  # never step back on corrections
		return self._reported


def _run[T](steps: Steps[T]) -> T:
	"""Run steps to the end without reporting progress."""
	while True:
		try:
			next(steps)
		except StopIteration as finished:
			return finished.value


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


def _table_steps(
	rows: Rows, headers: HeaderStyle, chars_per_unit: int, progress: Progress
) -> Steps[Table]:
	"""Flatten nested objects into columns, in first-seen key order."""
	flat: list[dict[str, Any]] = []
	keys: dict[str, Any] = {}
	blocks: list[float] = []
	block = 0.0
	long_cells = 0
	for index, row in enumerate(rows, start=1):
		flat_row = _flatten(row)
		keys.update(flat_row)
		chars = 0
		for value in flat_row.values():
			if isinstance(value, str):
				chars += len(value)
				long_cells += len(value) > EXCEL_MAX_CELL_CHARS
		block += len(flat_row) + chars / chars_per_unit
		flat.append(flat_row)
		if index % CHECKPOINT_ROWS == 0:
			blocks.append(block)
			block = 0.0
			progress.add("flatten", CHECKPOINT_ROWS)
			yield
	if len(flat) % CHECKPOINT_ROWS:
		blocks.append(block)
		progress.add("flatten", len(flat) % CHECKPOINT_ROWS)
	names = list(keys)
	columns = (
		[snake_to_pascal_case(key) for key in names] if headers == "readable" else names
	)
	return Table(columns, names, flat, blocks, long_cells)


def _tables_steps(
	sheets: Sheets, format_type: ExportFormat, headers: HeaderStyle, progress: Progress
) -> Steps[dict[str, Table]]:
	if format_type == "csv" and len(sheets) > 1:
		raise HTTPException(
			status_code=400,
			detail=f"CSV holds a single table, but this data has {len(sheets)} sheets. Choose Excel to keep them all.",
		)
	tables = {}
	for name, rows in sheets.items():
		if format_type == "excel" and len(rows) >= EXCEL_MAX_ROWS:
			raise _too_big_for_excel(name, len(rows), "rows")
		tables[name] = yield from _table_steps(
			rows, headers, CHARS_PER_UNIT[format_type], progress
		)
		if format_type == "excel" and len(tables[name].columns) > EXCEL_MAX_COLUMNS:
			raise _too_big_for_excel(name, len(tables[name].columns), "columns")
	progress.units = sum(sum(table.blocks) for table in tables.values())  # now exact
	return tables


def _too_big_for_excel(name: str, count: int, what: str) -> HTTPException:
	return HTTPException(
		status_code=400,
		detail=f"Sheet '{name}' has {count:,} {what}; Excel allows {EXCEL_MAX_ROWS - 1:,} rows "
		f"and {EXCEL_MAX_COLUMNS:,} columns. Export as CSV instead.",
	)


def build_tables(
	sheets: Sheets, format_type: ExportFormat, headers: HeaderStyle = "readable"
) -> dict[str, Table]:
	progress = Progress(0, STEP_COST[format_type], TYPICAL_UNITS_PER_ROW)
	return _run(_tables_steps(sheets, format_type, headers, progress))


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


def _excel_steps(tables: dict[str, Table], progress: Progress) -> Steps[bytes]:
	# XlsxWriter only deletes its temp files when the workbook is finished. A private
	# folder is removed even when the client leaves mid-way and the workbook is dropped.
	with tempfile.TemporaryDirectory() as tmpdir:
		buffer = BytesIO()
		workbook = xlsxwriter.Workbook(buffer, {**EXCEL_OPTIONS, "tmpdir": tmpdir})
		try:
			yield  # set up, nothing written yet
			yield from _excel_write_steps(workbook, tables, progress)
			progress.step = "finish"
			yield  # report the write step before zipping, which can't report progress
		except GeneratorExit:
			for sheet in workbook.worksheets():
				sheet._opt_close()  # close each sheet's temp file before it's deleted
			raise
		workbook.close()
	progress.add("finish", progress.units)
	return buffer.getvalue()


def _excel_write_steps(
	workbook: xlsxwriter.Workbook, tables: dict[str, Table], progress: Progress
) -> Steps[None]:
	header = workbook.add_format(
		{"bold": True, "font_color": "#FFFFFF", "bg_color": "#1F6F43"}
	)
	date_format = workbook.add_format({"num_format": "yyyy-mm-dd"})
	datetime_format = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
	for name, table in tables.items():
		ws = workbook.add_worksheet(name)
		if not table.columns:
			continue
		ws.freeze_panes(1, 0)
		ws.autofilter(0, 0, len(table.rows), len(table.columns) - 1)
		ws.write_row(0, 0, table.columns, header)
		widths = [len(column) for column in table.columns]
		for row_index, row in enumerate(table.rows, start=1):
			cells = table.cells(row)
			if row_index <= 1000:  # size columns from the first rows only
				for index, value in enumerate(cells):
					if value is not None:
						widths[index] = max(widths[index], len(str(value)))
			for column_index, value in enumerate(cells):
				if (when := _as_datetime(value)) is not None:
					cell_format = date_format if len(value) == 10 else datetime_format
					ws.write_datetime(row_index, column_index, when, cell_format)
				elif value is not None:
					ws.write(row_index, column_index, value)
			if row_index % CHECKPOINT_ROWS == 0:
				progress.add("write", table.blocks[row_index // CHECKPOINT_ROWS - 1])
				yield
		if len(table.rows) % CHECKPOINT_ROWS:
			progress.add("write", table.blocks[-1])
		for index, width in enumerate(widths):
			ws.set_column(index, index, min(60, width + 3))


def _csv_safe(value: Any) -> Any:
	"""Prefix text that Excel would run as a formula (OWASP CSV injection guidance)."""
	if (
		isinstance(value, str)
		and _CSV_FORMULA.match(value)
		and not _NUMBER.match(value)
	):
		return f"'{value}"
	return value


def _csv_steps(table: Table, progress: Progress) -> Steps[bytes]:
	buffer = StringIO()
	writer = csv.writer(buffer)
	writer.writerow([_csv_safe(column) for column in table.columns])
	yield  # set up, no rows written yet
	for index, row in enumerate(table.rows, start=1):
		writer.writerow([_csv_safe(value) for value in table.cells(row)])
		if index % CHECKPOINT_ROWS == 0:
			progress.add("write", table.blocks[index // CHECKPOINT_ROWS - 1])
			yield
	if len(table.rows) % CHECKPOINT_ROWS:
		progress.add("write", table.blocks[-1])
	progress.step = "finish"
	yield
	content = buffer.getvalue().encode("utf-8-sig")  # the BOM lets Excel read UTF-8
	progress.add("finish", progress.units)
	return content


def _export_steps(
	sheets: Sheets, format_type: ExportFormat, headers: HeaderStyle, progress: Progress
) -> Steps[ExportFile]:
	tables = yield from _tables_steps(sheets, format_type, headers, progress)
	if format_type == "csv":
		content = yield from _csv_steps(next(iter(tables.values())), progress)
		return ExportFile(content, "text/csv", "csv")
	content = yield from _excel_steps(tables, progress)
	return ExportFile(content, EXCEL_MEDIA_TYPE, "xlsx")


def _total_rows(sheets: Sheets) -> int:
	return sum(len(rows) for rows in sheets.values())


def _measure_costs(
	sheets: Sheets, format_type: ExportFormat, headers: HeaderStyle
) -> tuple[dict[str, float], float]:
	"""Time the real steps on rows spread through the data, so progress follows how this
	data converts. Returns step costs and work units per row."""
	total = _total_rows(sheets)
	if total < PROBE_ROWS * 10:
		return STEP_COST[format_type], TYPICAL_UNITS_PER_ROW
	stride = total // PROBE_ROWS
	sample = {name: rows[::stride] for name, rows in sheets.items()}
	count = _total_rows(sample)
	scratch = Progress(count, STEP_COST[format_type], TYPICAL_UNITS_PER_ROW)
	started = time.perf_counter()
	tables = _run(_tables_steps(sample, format_type, headers, scratch))
	flattened = time.perf_counter()
	writer = (
		_csv_steps(next(iter(tables.values())), scratch)
		if format_type == "csv"
		else _excel_steps(tables, scratch)
	)
	next(writer)  # set up the file outside the timing
	write_started = time.perf_counter()
	while scratch.step != "finish":  # write every row, stop before zipping
		next(writer)
	written = time.perf_counter()
	write = (written - write_started) / scratch.units
	if format_type == "excel":
		_run(writer)  # zip the sample too; that cost depends on the data's shape
		zipped = time.perf_counter() - written - _empty_zip_seconds()
		finish = max(0.0, zipped) / scratch.units
	else:
		writer.close()
		finish = write * CSV_ENCODE_SHARE
	cost = {"flatten": (flattened - started) / count, "write": write, "finish": finish}
	return cost, scratch.units / count


def _empty_zip_seconds() -> float:
	"""The fixed cost of zipping a workbook, to take out of the sample's zip time."""
	with tempfile.TemporaryDirectory() as tmpdir:
		workbook = xlsxwriter.Workbook(BytesIO(), {**EXCEL_OPTIONS, "tmpdir": tmpdir})
		workbook.add_worksheet()
		started = time.perf_counter()
		workbook.close()
		return time.perf_counter() - started


def _content_disposition(full_name: str) -> str:
	ascii_name = re.sub(r"[^\x20-\x7e]", "_", full_name)
	return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(full_name)}"


def _file_name(filename: str, extension: str) -> str:
	name = re.sub(r"\.(xlsx|csv)$", "", filename.strip(), flags=re.IGNORECASE)
	name = (
		re.sub(r'[\x00-\x1f\x7f"\\/:*?<>|]+', "_", name).strip(" .")[:100]
		or "exported_data"
	)
	return f"{name}.{extension}"


def _chunks(content: bytes, size: int) -> Iterator[bytes]:
	return (content[start : start + size] for start in range(0, len(content), size))


def export_data(
	sheets: Sheets,
	format_type: ExportFormat,
	filename: str,
	headers: HeaderStyle = "readable",
) -> StreamingResponse:
	"""Export sheets to an Excel workbook or a CSV file."""
	progress = Progress(
		_total_rows(sheets), STEP_COST[format_type], TYPICAL_UNITS_PER_ROW
	)
	file = _run(_export_steps(sheets, format_type, headers, progress))
	# Sent in chunks: Vercel's 4.5 MB response limit doesn't apply to streamed responses.
	return StreamingResponse(
		_chunks(file.content, 1024 * 1024),
		media_type=file.media_type,
		headers={
			"Content-Disposition": _content_disposition(
				_file_name(filename, file.extension)
			)
		},
	)


def event_line(**fields: Any) -> bytes:
	"""One line of the progress stream. ASCII-only JSON, so any decoder reads it."""
	return json.dumps(fields, separators=(",", ":")).encode() + b"\n"


def export_events(
	text: str, format_type: ExportFormat, filename: str, headers: HeaderStyle
) -> Generator[bytes, None, None]:
	"""The progress stream: JSON lines with progress events, then the file in base64 chunks.

	Events: {"stage": "reading"}, then {"stage": "converting", "step": "flatten" | "write"
	| "finish", "progress": 0.0-1.0, "seconds_left": s or null until steady, "rows": n},
	then {"stage": "file", "name", "size", "type"} and {"data": base64} lines.
	{"stage": "error", "detail"} ends a failed stream.
	"""
	try:
		yield event_line(stage="reading")
		sheets = validate_and_parse_data(text)
		cost, units_per_row = _measure_costs(sheets, format_type, headers)
		progress = Progress(_total_rows(sheets), cost, units_per_row)
		steps = _export_steps(sheets, format_type, headers, progress)
		rows, reported_step, reported_at = progress.total_rows, "", 0.0
		while True:
			# Report each new step at once, and progress within a step every 0.1 s.
			now = time.monotonic()
			if progress.step != reported_step or now - reported_at >= PROGRESS_INTERVAL:
				reported_step, reported_at = progress.step, now
				yield event_line(
					stage="converting",
					step=progress.step,
					progress=round(progress.fraction, 4),
					seconds_left=(
						round(progress.seconds_left(), 1) if progress.settled else None
					),
					rows=rows,
				)
			try:
				next(steps)
			except StopIteration as finished:
				file: ExportFile = finished.value
				break
	except HTTPException as error:
		yield event_line(stage="error", detail=error.detail)
		return
	except Exception:
		yield event_line(stage="error", detail="Something went wrong on the server.")
		raise  # still logged as a server error
	yield event_line(
		stage="converting", step="finish", progress=1.0, seconds_left=0.0, rows=rows
	)
	yield event_line(
		stage="file",
		name=_file_name(filename, file.extension),
		size=len(file.content),
		type=file.media_type,
	)
	for chunk in _chunks(file.content, FILE_CHUNK_BYTES):
		yield event_line(data=base64.b64encode(chunk).decode("ascii"))


def preview_data(
	sheets: Sheets,
	format_type: ExportFormat,
	headers: HeaderStyle = "readable",
	limit: int = 20,
) -> dict[str, Any]:
	"""Describe what the export will contain: per sheet, its columns and first rows."""
	tables = build_tables(sheets, format_type, headers)
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
				"rows": [table.cells(row) for row in table.rows[:limit]],
				"total_rows": len(table.rows),
			}
			for name, table in tables.items()
		],
		"warnings": warnings,
	}
