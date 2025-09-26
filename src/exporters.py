"""
Export functionality for converting data to Excel and CSV formats.
"""

from io import BytesIO
from typing import List, Dict, Any
import pandas as pd
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from .validators import snake_to_pascal_case


def export_to_excel(data: List[Dict[str, Any]], filename: str) -> StreamingResponse:
    """Export data to Excel format."""
    try:
        df = pd.DataFrame(data)
        df.columns = [snake_to_pascal_case(col) for col in df.columns]

        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Data", index=False)
        buffer.seek(0)

        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        file_extension = "xlsx"
        full_filename = f"{filename}.{file_extension}"

        return StreamingResponse(
            BytesIO(buffer.getvalue()),
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={full_filename}"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def export_to_csv(data: List[Dict[str, Any]], filename: str) -> StreamingResponse:
    """Export data to CSV format."""
    try:
        df = pd.DataFrame(data)
        df.columns = [snake_to_pascal_case(col) for col in df.columns]

        buffer = BytesIO()
        df.to_csv(buffer, index=False, encoding="utf-8")
        buffer.seek(0)

        media_type = "text/csv"
        file_extension = "csv"
        full_filename = f"{filename}.{file_extension}"

        return StreamingResponse(
            BytesIO(buffer.getvalue()),
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={full_filename}"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def export_data(
    data: List[Dict[str, Any]], format_type: str, filename: str
) -> StreamingResponse:
    """Export data to specified format."""
    if format_type.lower() == "csv":
        return export_to_csv(data, filename)
    else:
        return export_to_excel(data, filename)
