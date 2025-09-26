"""
API routes for the JSON to Excel/CSV converter.
"""

import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .models import ExportRequest
from .validators import validate_and_parse_data
from .exporters import export_data

templates = Jinja2Templates(directory="templates")

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the main HTML interface."""
    return templates.TemplateResponse("index.html", {"request": request})


@router.post("/export")
async def export_data_endpoint(request: ExportRequest):
    """Export data to Excel or CSV format."""
    try:
        data = validate_and_parse_data(request.data)
        return export_data(data, request.format, request.filename)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/export-raw")
async def export_data_raw(request: Request):
    """Export data from raw request body (for large data)."""
    try:
        body = await request.body()

        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="Invalid JSON format in request body."
            )

        format_type = request.query_params.get("format", "excel")
        filename = request.query_params.get("filename", "exported_data")

        data = validate_and_parse_data(data)
        return export_data(data, format_type, filename)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
