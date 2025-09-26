import json
import ast
from io import BytesIO
from typing import Union, List, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import pandas as pd
from pydantic import BaseModel

app = FastAPI(title="JSON to Excel/CSV Converter")

templates = Jinja2Templates(directory="templates")


class ExportRequest(BaseModel):
    data: Union[str, List[Dict[str, Any]]]
    format: str = "excel"
    filename: str = "exported_data"


def validate_and_parse_data(
    data: Union[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Validate and parse input data."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            try:
                data = ast.literal_eval(data)
            except (ValueError, SyntaxError):
                try:
                    converted_data = data.replace("'", '"')
                    data = json.loads(converted_data)
                except json.JSONDecodeError:
                    try:
                        decoded_data = json.loads(data)
                        if isinstance(decoded_data, str):
                            data = json.loads(decoded_data)
                        else:
                            data = decoded_data
                    except (json.JSONDecodeError, TypeError):
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid data format. Please use valid JSON format with double quotes, Python dictionary format, or stringified JSON.",
                        )

    if not isinstance(data, list):
        raise HTTPException(
            status_code=400,
            detail="Data must be a list of dictionaries or a JSON string representing a list.",
        )

    if not data:
        raise HTTPException(
            status_code=400, detail="No data to export. The provided data is empty."
        )

    if not all(isinstance(item, dict) for item in data):
        raise HTTPException(
            status_code=400, detail="All items in the data list must be dictionaries."
        )

    return data


def snake_to_pascal_case(snake_str: str) -> str:
    """Convert snake_case to Pascal Case with spaces."""
    components = snake_str.split("_")
    return " ".join(word.capitalize() for word in components)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the main HTML interface."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/export")
async def export_data(request: ExportRequest):
    """Export data to Excel or CSV format."""
    try:
        data = validate_and_parse_data(request.data)

        df = pd.DataFrame(data)
        df.columns = [snake_to_pascal_case(col) for col in df.columns]

        if request.format.lower() == "csv":
            buffer = BytesIO()
            df.to_csv(buffer, index=False, encoding="utf-8")
            buffer.seek(0)

            media_type = "text/csv"
            file_extension = "csv"
        else:
            buffer = BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name="Data", index=False)
            buffer.seek(0)

            media_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            file_extension = "xlsx"

        filename = f"{request.filename}.{file_extension}"

        return StreamingResponse(
            BytesIO(buffer.getvalue()),
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/export-raw")
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

        df = pd.DataFrame(data)

        df.columns = [snake_to_pascal_case(col) for col in df.columns]

        if format_type.lower() == "csv":
            buffer = BytesIO()
            df.to_csv(buffer, index=False, encoding="utf-8")
            buffer.seek(0)

            media_type = "text/csv"
            file_extension = "csv"
        else:
            buffer = BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name="Data", index=False)
            buffer.seek(0)

            media_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            file_extension = "xlsx"

        filename = f"{filename}.{file_extension}"

        return StreamingResponse(
            BytesIO(buffer.getvalue()),
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
