"""
Pydantic models for request/response validation.
"""

from typing import Any

from pydantic import BaseModel


class ExportRequest(BaseModel):
	"""Request model for export endpoint."""

	data: str | list[dict[str, Any]]
	format: str = "excel"
	filename: str = "exported_data"
