"""The /export-progress stream: progress events, then the file."""

import base64
import gzip
from io import BytesIO
import json
import tempfile
import threading
import time

import anyio
from fastapi.testclient import TestClient
import httpx
from openpyxl import load_workbook
import pytest
import uvicorn

from main import app
import src.exporters as exporters
import src.routes as routes

client = TestClient(app)

STUDENTS = [
	{"student_name": "John Doe", "grade": "A", "credits": 3},
	{"student_name": "Jane Smith", "grade": "B+", "credits": 4},
]


def many_rows(count):
	return [
		{"id": i, "name": f"Student {i}", "joined": "2024-01-15"} for i in range(count)
	]


def stream(data, fmt="excel", filename="out", **headers):
	text = data if isinstance(data, str | bytes) else json.dumps(data)
	response = client.post(
		f"/export-progress?format={fmt}&filename={filename}",
		content=text,
		headers=headers,
	)
	assert response.status_code == 200, response.text
	assert response.headers["content-type"] == "application/x-ndjson"
	return [json.loads(line) for line in response.text.splitlines()]


def file_bytes(events):
	header = next(event for event in events if event.get("stage") == "file")
	content = b"".join(base64.b64decode(e["data"]) for e in events if "data" in e)
	assert len(content) == header["size"]
	return header, content


def test_events_arrive_in_order_and_progress_never_goes_back(monkeypatch):
	monkeypatch.setattr(exporters, "PROGRESS_INTERVAL", 0)  # report at every pause
	events = stream(many_rows(6000))  # big enough to time a sample first
	stages = [event.get("stage", "data") for event in events]
	assert stages[0] == "reading"
	assert stages.index("file") > stages.index("converting")
	converting = [event for event in events if event.get("stage") == "converting"]
	progress = [event["progress"] for event in converting]
	assert progress == sorted(progress)
	assert progress[0] < 0.2 and progress[-1] == 1.0
	assert len(converting) > 10
	steps = [event["step"] for event in converting]
	assert steps.index("flatten") < steps.index("write") < steps.index("finish")
	assert converting[-1]["seconds_left"] == 0
	assert all(event["rows"] == 6000 for event in converting)


def test_streamed_excel_file_matches_the_data():
	header, content = file_bytes(stream(STUDENTS, filename="تقرير"))
	assert header["name"] == "تقرير.xlsx"
	assert header["type"] == exporters.EXCEL_MEDIA_TYPE
	ws = load_workbook(BytesIO(content))["Data"]
	assert [cell.value for cell in ws[2]] == ["John Doe", "A", 3]


def test_streamed_csv_is_identical_to_a_plain_export():
	rows = many_rows(1200)
	_, content = file_bytes(stream(rows, fmt="csv"))
	plain = client.post("/export-raw?format=csv", content=json.dumps(rows))
	assert content == plain.content


def test_gzip_upload():
	compressed = gzip.compress(json.dumps(STUDENTS).encode())
	events = stream(compressed, **{"Content-Encoding": "gzip"})
	assert any(event.get("stage") == "file" for event in events)


def test_bad_data_ends_the_stream_with_an_error():
	events = stream('[{"a": 1 "b": 2}]')
	assert events[0] == {"stage": "reading"}
	assert events[-1]["stage"] == "error"
	assert "line 1, column 10" in events[-1]["detail"]
	assert not any(event.get("stage") == "file" for event in events)


def test_waits_for_a_free_slot(monkeypatch):
	class BusySlots:
		def acquire_on_behalf_of_nowait(self, borrower):
			raise anyio.WouldBlock

		async def acquire_on_behalf_of(self, borrower):
			pass

		def release_on_behalf_of(self, borrower):
			pass

	monkeypatch.setattr("src.routes._heavy_slots", BusySlots())
	monkeypatch.setattr("src.routes.HEAVY_BYTES", 0)
	events = stream(STUDENTS)
	assert events[0] == {"stage": "queued"}
	assert events[-1].get("data")


@pytest.fixture
def live_server():
	"""A real uvicorn server, since dropped connections can't be faked in TestClient."""
	server = uvicorn.Server(
		uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
	)
	thread = threading.Thread(target=server.run, daemon=True)
	thread.start()
	while not server.started:
		time.sleep(0.01)
	port = server.servers[0].sockets[0].getsockname()[1]
	yield f"http://127.0.0.1:{port}"
	server.should_exit = True
	thread.join(timeout=10)


def test_cancelled_exports_give_back_their_slot(live_server, monkeypatch):
	monkeypatch.setattr("src.routes.HEAVY_BYTES", 0)  # every export needs a slot
	body = json.dumps(many_rows(20_000))
	for _ in range(3):  # more cancels than there are slots
		with httpx.stream(
			"POST", f"{live_server}/export-progress", content=body
		) as reply:
			for line in reply.iter_lines():
				if '"stage":"converting"' in line:
					break  # leave mid-way, like the Cancel button
	deadline = time.monotonic() + 5
	while routes._heavy_slots.borrowed_tokens and time.monotonic() < deadline:
		time.sleep(0.05)
	assert routes._heavy_slots.borrowed_tokens == 0
	events = stream(STUDENTS)
	assert events[0] == {"stage": "reading"}  # not stuck waiting for a slot


def test_temp_files_are_removed_when_finished_or_cancelled(monkeypatch, tmp_path):
	monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
	text = json.dumps(many_rows(6000))

	finished = list(exporters.export_events(text, "excel", "x", "readable"))
	assert finished[-1].startswith(b'{"data"')
	assert list(tmp_path.iterdir()) == []

	cancelled = exporters.export_events(text, "excel", "x", "readable")
	for line in cancelled:  # leave while rows are being written
		if b'"step":"write"' in line:
			break
	assert list(tmp_path.iterdir())  # the workbook's temp folder exists mid-way
	cancelled.close()
	assert list(tmp_path.iterdir()) == []
