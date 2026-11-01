# JSON to Excel/CSV Converter

[![CI](https://github.com/dhiashalabi/fastapi-json-to-xlsx/actions/workflows/ci.yml/badge.svg)](https://github.com/dhiashalabi/fastapi-json-to-xlsx/actions/workflows/ci.yml)

Turn JSON into a formatted Excel workbook or a CSV file. A FastAPI app with a web UI that previews the result before you download it.

- One sheet per key: `{"Students": [...], "Courses": [...]}` becomes a two-sheet workbook.
- Nested objects become columns (`address.city` → **Address City**); ISO dates become real Excel dates.
- Accepts JSON, JSON Lines, and Python-style literals. Gzip uploads are supported for large data.
- Safe output: no live formulas, CSV injection escaping, and long IDs kept exact.

## Run

```bash
uv sync
uv run fastapi dev main.py
```

Open <http://localhost:8000>. Interactive API docs are at `/docs`.

## API

| Endpoint | Body | Returns |
| --- | --- | --- |
| `POST /export` | `{"data": ..., "format": "excel", "filename": "report", "headers": "readable"}` | The file |
| `POST /export-raw?format=csv&filename=report` | The data itself (optionally gzip) | The file |
| `POST /preview?format=excel` | The data itself (optionally gzip) | Sheets, columns, first 20 rows, warnings |

`format` is `excel` or `csv`. `headers` is `readable` (`student_name` → `Student Name`) or `original`.

```bash
gzip -c data.json | curl -X POST "http://localhost:8000/export-raw?format=excel&filename=report" \
  -H "Content-Encoding: gzip" --data-binary @- -o report.xlsx
```

## Limits

| Limit | Value |
| --- | --- |
| Request size | 100 MB unpacked by default (set `MAX_DATA_MB`); memory use is about 7.5× that |
| On Vercel | 4.5 MB per upload after gzip (the UI compresses for you), 300 s, 2 GB memory (Hobby). Downloads are streamed, so they aren't held to that 4.5 MB cap |
| Excel | 1,048,575 rows, 16,384 columns, 32,767 characters per cell (the preview warns) |
| Speed | 20 MB of JSON (95k rows × 9 columns): about 12 s as Excel, 3 s as CSV |
| At once | Up to 2 big conversions (over 1 MB) run at a time; others wait their turn |

## Develop

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```

Deploys to Vercel with zero config (`main.py` + `pyproject.toml`), and `public/` is served from the CDN. For a public site, add a rate-limit rule in Vercel's Firewall settings.

MIT, see [LICENSE](LICENSE).
