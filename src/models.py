"""
Pydantic models for request/response validation.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

ExportFormat = Literal["excel", "csv"]
HeaderStyle = Literal["readable", "original"]


class ExportRequest(BaseModel):
	"""Request model for the export endpoint."""

	data: str | list[Any] | dict[str, Any] = Field(
		description="A JSON array of objects, an object of arrays (one sheet per key), "
		"a single object, or any of these as a JSON, JSON Lines or Python-literal string."
	)
	format: ExportFormat = "excel"
	filename: str = Field("exported_data", max_length=200)
	headers: HeaderStyle = Field(
		"readable",
		description='"readable" turns student_name into "Student Name"; "original" keeps keys as they are.',
	)
