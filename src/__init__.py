"""
JSON to Excel/CSV Converter Package

A FastAPI-based web application that converts JSON data into Excel (.xlsx) or CSV (.csv) format.
"""

from .exporters import build_table, export_data, preview_data
from .models import ExportRequest
from .routes import router
from .validators import snake_to_pascal_case, validate_and_parse_data

__version__ = "0.3.0"
__all__ = [
	"ExportRequest",
	"validate_and_parse_data",
	"snake_to_pascal_case",
	"build_table",
	"export_data",
	"preview_data",
	"router",
]
