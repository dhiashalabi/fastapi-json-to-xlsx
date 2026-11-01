"""
API routes for the JSON to Excel/CSV converter.
"""

from collections.abc import Callable
import os
from typing import Any
import zlib

import anyio
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import iterate_in_threadpool
from fastapi.responses import StreamingResponse

from .exporters import event_line, export_data, export_events, preview_data
from .models import ExportFormat, ExportRequest, HeaderStyle
from .validators import validate_and_parse_data

# Unpacked request size limit. Memory use is about 7.5x this while converting.
MAX_DATA_BYTES = int(os.environ.get("MAX_DATA_MB", "100")) * 1_000_000
# Conversions are CPU-bound, so under the GIL running more at once only adds memory.
# Big ones wait for one of 2 slots; small ones skip the queue.
HEAVY_BYTES = 1_000_000
_heavy_slots = anyio.CapacityLimiter(2)
# Lets /docs show a body box for the endpoints that read the raw body themselves.
RAW_BODY_DOCS = {
	"requestBody": {
		"required": True,
		"description": "JSON, JSON Lines or Python-literal data. May be gzip-compressed "
		"(send `Content-Encoding: gzip`).",
		"content": {"application/json": {"schema": {}}},
	}
}

router = APIRouter()


def too_large() -> HTTPException:
	limit = MAX_DATA_BYTES / 1_000_000
	return HTTPException(
		status_code=413, detail=f"Data is larger than this server's {limit:g} MB limit."
	)


async def read_body(request: Request) -> str:
	"""Read the body as text, unpacking gzip, without ever holding more than the limit."""
	body = bytearray()
	async for chunk in request.stream():
		body += chunk
		if len(body) > MAX_DATA_BYTES:
			raise too_large()
	encoding = request.headers.get("content-encoding", "identity").lower()
	if encoding == "gzip":
		inflater = zlib.decompressobj(wbits=31)
		try:
			body = inflater.decompress(body, MAX_DATA_BYTES + 1)
		except zlib.error as e:
			raise HTTPException(
				status_code=400, detail="Request body is not valid gzip."
			) from e
		if len(body) > MAX_DATA_BYTES:
			raise too_large()
		if not inflater.eof or inflater.unused_data:
			raise HTTPException(
				status_code=400,
				detail="Request body is incomplete or has several gzip parts.",
			)
	elif encoding != "identity":
		raise HTTPException(
			status_code=415,
			detail=f"Unsupported Content-Encoding '{encoding}'. Send plain text or gzip.",
		)
	try:
		return body.decode("utf-8")
	except UnicodeDecodeError as e:
		raise HTTPException(
			status_code=400, detail="Request body must be UTF-8 text."
		) from e


async def run_conversion[T](size: int, job: Callable[[], T]) -> T:
	"""Run a conversion in a worker thread so the server stays responsive."""
	return await anyio.to_thread.run_sync(
		job, limiter=_heavy_slots if size > HEAVY_BYTES else None
	)


@router.post("/export")
async def export_data_endpoint(
	payload: ExportRequest, request: Request
) -> StreamingResponse:
	"""Export data to an Excel workbook (one sheet per table) or a CSV file."""
	size = int(request.headers.get("content-length") or 0)
	return await run_conversion(
		size,
		lambda: export_data(
			validate_and_parse_data(payload.data),
			payload.format,
			payload.filename,
			payload.headers,
		),
	)


@router.post("/export-raw", openapi_extra=RAW_BODY_DOCS)
async def export_data_raw(
	request: Request,
	format_type: ExportFormat = Query("excel", alias="format"),
	filename: str = Query("exported_data", max_length=200),
	headers: HeaderStyle = "readable",
) -> StreamingResponse:
	"""Export data sent as the raw request body (best for large data and curl)."""
	text = await read_body(request)
	return await run_conversion(
		len(text),
		lambda: export_data(
			validate_and_parse_data(text), format_type, filename, headers
		),
	)


@router.post("/export-progress", openapi_extra=RAW_BODY_DOCS)
async def export_with_progress(
	request: Request,
	format_type: ExportFormat = Query("excel", alias="format"),
	filename: str = Query("exported_data", max_length=200),
	headers: HeaderStyle = "readable",
) -> StreamingResponse:
	"""Like /export-raw, but streams JSON lines: progress events, then the file in
	base64 chunks. Starts with {"stage": "queued"} when it has to wait for a free slot."""
	text = await read_body(request)
	heavy = len(text) > HEAVY_BYTES

	async def stream():
		# Held under a token of its own: if the client leaves, this generator may be
		# cleaned up by another task, and the slot must still be released.
		slot = object()
		if heavy:
			try:
				_heavy_slots.acquire_on_behalf_of_nowait(slot)
			except anyio.WouldBlock:
				yield event_line(stage="queued")
				await _heavy_slots.acquire_on_behalf_of(slot)
		lines = export_events(text, format_type, filename, headers)
		try:
			async for line in iterate_in_threadpool(lines):
				if await request.is_disconnected():
					break  # cancelled: stop converting now, not when the file is done
				yield line
		finally:
			lines.close()  # stops unfinished work and deletes its temp files
			if heavy:
				_heavy_slots.release_on_behalf_of(slot)

	return StreamingResponse(
		stream(),
		media_type="application/x-ndjson",
		headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
	)


@router.post("/preview", openapi_extra=RAW_BODY_DOCS)
async def preview_endpoint(
	request: Request,
	format_type: ExportFormat = Query("excel", alias="format"),
	headers: HeaderStyle = "readable",
) -> dict[str, Any]:
	"""Show the sheets, columns, first 20 rows and any warnings an export would produce."""
	text = await read_body(request)
	return await run_conversion(
		len(text),
		lambda: preview_data(validate_and_parse_data(text), format_type, headers),
	)
