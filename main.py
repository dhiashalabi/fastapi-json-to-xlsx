"""
Main FastAPI application entry point for JSON to Excel/CSV converter.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES

from src import __version__, routes
from src.exporters import EXCEL_MEDIA_TYPE

app = FastAPI(title="JSON to Excel/CSV Converter", version=__version__)

# Compress responses (CSV shrinks 5-10x); .xlsx files are already zipped.
app.add_middleware(
	GZipMiddleware,
	compresslevel=6,
	exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, EXCEL_MEDIA_TYPE),
)


@app.middleware("http")
async def reject_oversized_bodies(request: Request, call_next):
	"""Refuse bodies over the limit before reading them (routes.read_body also caps streamed ones)."""
	if int(request.headers.get("content-length") or 0) > routes.MAX_DATA_BYTES:
		return JSONResponse({"detail": routes.too_large().detail}, status_code=413)
	return await call_next(request)


app.include_router(routes.router)
# Serves the web UI. On Vercel, public/ is served straight from the CDN and may be left
# out of the function bundle, so a missing folder must not stop the API from starting.
app.frontend(
	"/", directory=Path(__file__).parent / "public", fallback=None, check_dir=False
)
