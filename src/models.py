"""
Pydantic models for request/response validation.
"""

from typing import Union, List, Dict, Any
from pydantic import BaseModel


class ExportRequest(BaseModel):
    """Request model for export endpoint."""

    data: Union[str, List[Dict[str, Any]]]
    format: str = "excel"
    filename: str = "exported_data"
