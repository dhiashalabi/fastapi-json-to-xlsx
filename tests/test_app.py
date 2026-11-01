import codecs
from datetime import datetime
import gzip
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import sys

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest

from main import app
from src.validators import snake_to_pascal_case

client = TestClient(app)

STUDENTS = [
	{"student_name": "John Doe", "grade": "A", "credits": 3},
	{"student_name": "Jane Smith", "grade": "B+", "credits": 4},
]


def export(data, fmt="excel", filename="out", headers="readable"):
	body = {"data": data, "format": fmt, "filename": filename, "headers": headers}
	return client.post("/export", json=body)


def preview(data, fmt="excel", **kwargs):
	text = data if isinstance(data, str | bytes) else json.dumps(data)
	return client.post(f"/preview?format={fmt}", content=text, **kwargs)


def workbook(response):
	assert response.status_code == 200, response.text
	return load_workbook(BytesIO(response.content))


def values(ws):
	return [[cell.value for cell in row] for row in ws.iter_rows()]


def csv_lines(response):
	assert response.status_code == 200, response.text
	return response.content.decode("utf-8-sig").splitlines()


def test_index_page():
	response = client.get("/")
	assert response.status_code == 200
	assert response.headers["content-type"].startswith("text/html")
	assert "JSON to Excel" in response.text


def test_unknown_path_is_404():
	assert client.get("/nope", headers={"Accept": "text/html"}).status_code == 404


def test_excel_export_is_formatted():
	response = export(STUDENTS, filename="students")
	assert response.headers["content-disposition"].startswith(
		'attachment; filename="students.xlsx"'
	)
	ws = workbook(response)["Data"]
	assert values(ws) == [
		["Student Name", "Grade", "Credits"],
		["John Doe", "A", 3],
		["Jane Smith", "B+", 4],
	]
	assert ws["A1"].font.bold
	assert ws.freeze_panes == "A2"
	assert ws.auto_filter.ref == "A1:C3"


def test_original_headers_keep_keys():
	ws = workbook(
		export(
			[{"student_name": "Ann", "address": {"city": "Amman"}}], headers="original"
		)
	)
	assert values(ws["Data"])[0] == ["student_name", "address.city"]


def test_json_string_input():
	assert values(workbook(export(json.dumps(STUDENTS)))["Data"])[1][0] == "John Doe"


def test_json_lines_input():
	text = '{"name": "Ann", "age": 30}\n\n{"name": "Omar", "age": 25}\n'
	assert values(workbook(export(text))["Data"]) == [
		["Name", "Age"],
		["Ann", 30],
		["Omar", 25],
	]


def test_json_lines_bad_line_is_reported():
	response = export('{"a": 1}\n{"a": }\n')
	assert response.status_code == 400
	assert "line 2" in response.json()["detail"]


def test_object_of_arrays_becomes_one_sheet_per_key():
	data = {"Students": STUDENTS, "Courses": [{"title": "Math"}], "a/b:c*?": [{"x": 1}]}
	wb = workbook(export(data))
	assert wb.sheetnames == ["Students", "Courses", "a_b_c__"]
	assert values(wb["Courses"]) == [["Title"], ["Math"]]


def test_duplicate_and_long_sheet_names_stay_valid():
	data = {"x" * 40: [{"a": 1}], "X" * 40: [{"a": 2}]}
	assert workbook(export(data)).sheetnames == ["x" * 31, "X" * 27 + " (2)"]


def test_single_object_is_one_row():
	ws = workbook(export({"name": "Ann", "age": 30}))["Data"]
	assert values(ws) == [["Name", "Age"], ["Ann", 30]]


def test_nested_objects_flatten_and_lists_become_json():
	data = [
		{
			"name": "Ann",
			"address": {"city": "Amman", "geo": {"lat": 31.9}},
			"tags": ["a", "b"],
		}
	]
	assert values(workbook(export(data))["Data"]) == [
		["Name", "Address City", "Address Geo Lat", "Tags"],
		["Ann", "Amman", 31.9, '["a", "b"]'],
	]


def test_python_style_input_with_js_literals():
	text = "[{'name': \"O'Brien\", 'active': true, 'note': null}]"
	assert values(workbook(export(text))["Data"]) == [
		["Name", "Active", "Note"],
		["O'Brien", True, None],
	]


def test_double_encoded_json():
	ws = workbook(export(json.dumps(json.dumps(STUDENTS))))["Data"]
	assert values(ws)[2][0] == "Jane Smith"


def test_iso_dates_become_real_excel_dates():
	row = {
		"day": "2024-01-15",
		"utc": "2024-01-15T10:30:00Z",
		"offset": "2024-01-15T10:30:00+03:00",
		"not_a_date": "2024-02-30",
	}
	ws = workbook(export([row]))["Data"]
	assert ws["A2"].value == datetime(2024, 1, 15)
	assert ws["A2"].number_format == "yyyy-mm-dd"
	assert ws["B2"].value == datetime(2024, 1, 15, 10, 30)
	assert ws["B2"].number_format == "yyyy-mm-dd hh:mm:ss"
	assert ws["C2"].value == "2024-01-15T10:30:00+03:00"
	assert ws["D2"].value == "2024-02-30"


def test_long_integers_stay_exact():
	data = [{"id": 12345678901234567890, "small": 123456789012345}]
	assert values(workbook(export(data))["Data"])[1] == [
		"12345678901234567890",
		123456789012345,
	]
	assert preview(data).json()["sheets"][0]["rows"][0] == [
		"12345678901234567890",
		123456789012345,
	]


@pytest.mark.parametrize(
	("data", "message"),
	[
		("[{'a': 1}", "Invalid JSON"),
		("", "empty"),
		([], "empty"),
		("[1, 2]", "must be objects"),
		("42", "must be a JSON array"),
	],
)
def test_bad_input_is_400_with_clear_message(data, message):
	response = export(data)
	assert response.status_code == 400
	assert message in response.json()["detail"]


def test_invalid_json_reports_position():
	assert "line 1, column 10" in export('[{"a": 1 "b": 2}]').json()["detail"]


def test_unknown_format_is_rejected():
	assert export(STUDENTS, fmt="pdf").status_code == 422


def test_non_ascii_filename():
	response = export(STUDENTS, filename="تقرير الطلاب")
	assert response.status_code == 200
	assert "filename*=UTF-8''%D8%AA" in response.headers["content-disposition"]


def test_filename_is_sanitized_and_extension_not_doubled():
	response = export(STUDENTS, fmt="csv", filename='../evil"name.csv')
	assert 'filename="_evil_name.csv"' in response.headers["content-disposition"]


def test_excel_formulas_are_stored_as_text():
	ws = workbook(export([{"f": '=HYPERLINK("http://x")', "e": "#N/A"}]))["Data"]
	assert ws["A2"].value == '=HYPERLINK("http://x")'
	assert ws["A2"].data_type == "s"
	assert ws["B2"].data_type == "s"


def test_csv_escapes_formulas_keeps_numbers_and_has_bom():
	data = [{"f": "=1+1", "n": "-5", "at": "@SUM(A1)", "i": 1}, {"i": 2}]
	response = export(data, fmt="csv")
	assert response.content.startswith(codecs.BOM_UTF8)
	assert csv_lines(response) == ["F,N,At,I", "'=1+1,-5,'@SUM(A1),1", ",,,2"]


def test_csv_rejects_multiple_sheets():
	response = export({"A": [{"x": 1}], "B": [{"y": 2}]}, fmt="csv")
	assert response.status_code == 400
	assert "Choose Excel" in response.json()["detail"]


def test_control_characters_are_stripped():
	assert values(workbook(export([{"a": "x\x01y"}]))["Data"])[1] == ["xy"]


def test_too_many_rows_for_excel(monkeypatch):
	monkeypatch.setattr("src.exporters.EXCEL_MAX_ROWS", 2)
	assert "Export as CSV" in export(STUDENTS).json()["detail"]
	assert preview(STUDENTS).status_code == 400  # the preview warns before any export


def test_preview():
	rows = [{"n": i, "nested": {"ok": i % 2 == 0}} for i in range(25)]
	response = preview({"Numbers": rows, "Empty": []})
	assert response.status_code == 200
	body = response.json()
	first, second = body["sheets"]
	assert first["name"] == "Numbers"
	assert first["columns"] == ["N", "Nested Ok"]
	assert first["total_rows"] == 25
	assert len(first["rows"]) == 20
	assert first["rows"][1] == [1, False]
	assert second == {"name": "Empty", "columns": [], "rows": [], "total_rows": 0}
	assert body["warnings"] == []


def test_preview_warns_about_cells_too_long_for_excel():
	data = [{"text": "x" * 40_000}]
	assert "will be cut" in preview(data).json()["warnings"][0]
	assert preview(data, fmt="csv").json()["warnings"] == []


def test_export_raw_body():
	response = client.post(
		"/export-raw?format=csv&filename=raw",
		content='[{"name": "John", "age": 30}]',
		headers={"Content-Type": "text/plain"},
	)
	assert csv_lines(response) == ["Name,Age", "John,30"]
	assert 'filename="raw.csv"' in response.headers["content-disposition"]


def test_export_raw_rejects_bad_json():
	assert client.post("/export-raw", content="nope").status_code == 400


def test_gzip_request_body():
	body = gzip.compress(json.dumps(STUDENTS).encode())
	headers = {"Content-Encoding": "gzip"}
	response = client.post("/export-raw?format=csv", content=body, headers=headers)
	assert csv_lines(response)[1] == "John Doe,A,3"
	assert preview(body, headers=headers).json()["sheets"][0]["total_rows"] == 2


def test_bad_gzip_and_unknown_encoding_are_rejected():
	bad = client.post(
		"/preview", content=b"not gzip", headers={"Content-Encoding": "gzip"}
	)
	assert bad.status_code == 400
	truncated = gzip.compress(json.dumps(STUDENTS).encode())[:-8]
	assert preview(truncated, headers={"Content-Encoding": "gzip"}).status_code == 400
	assert preview("[]", headers={"Content-Encoding": "br"}).status_code == 415


def test_size_limits(monkeypatch):
	monkeypatch.setattr("src.routes.MAX_DATA_BYTES", 1000)
	big = json.dumps([{"a": "x" * 2000}])
	assert preview(big).status_code == 413  # rejected by Content-Length
	assert export([{"a": "x" * 2000}]).status_code == 413
	bomb = gzip.compress(
		b"[" + b" " * 100_000 + b"]"
	)  # small upload, huge once unpacked
	assert len(bomb) < 1000
	response = preview(bomb, headers={"Content-Encoding": "gzip"})
	assert response.status_code == 413
	assert (
		response.json()["detail"] == "Data is larger than this server's 0.001 MB limit."
	)


def test_responses_are_gzipped_except_xlsx():
	rows = [{"name": f"Student {i}", "note": "same text again"} for i in range(200)]
	assert export(rows, fmt="csv").headers.get("content-encoding") == "gzip"
	assert "content-encoding" not in export(rows).headers


def test_downloads_are_streamed():
	# A streamed response has no Content-Length, which keeps it clear of Vercel's 4.5 MB cap.
	assert "content-length" not in export(STUDENTS).headers
	assert "content-length" not in export(STUDENTS, fmt="csv").headers


def test_big_conversions_take_the_queue(monkeypatch):
	monkeypatch.setattr("src.routes.HEAVY_BYTES", 0)  # treat every request as big
	assert values(workbook(export(STUDENTS))["Data"])[1][0] == "John Doe"
	assert preview(STUDENTS).json()["sheets"][0]["total_rows"] == 2


def test_api_starts_without_public_folder(tmp_path):
	"""On Vercel, public/ may be served by the CDN and missing from the function bundle."""
	root = Path(__file__).parent.parent
	shutil.copy(root / "main.py", tmp_path)
	shutil.copytree(root / "src", tmp_path / "src")
	check = "from fastapi.testclient import TestClient; import main; print(TestClient(main.app).post('/preview', content='[{\"a\": 1}]').status_code)"
	result = subprocess.run(
		[sys.executable, "-c", check], cwd=tmp_path, capture_output=True, text=True
	)
	assert result.stdout.strip() == "200", result.stderr


@pytest.mark.parametrize(
	("key", "header"),
	[
		("student_name", "Student Name"),
		("studentName", "Student Name"),
		("userID", "User ID"),
		("HTTPServer", "HTTP Server"),
		("address.city", "Address City"),
		("GPA", "GPA"),
		("الاسم", "الاسم"),
	],
)
def test_header_names(key, header):
	assert snake_to_pascal_case(key) == header
