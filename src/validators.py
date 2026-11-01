"""
Data validation and parsing utilities for JSON to Excel/CSV converter.
"""

import ast
import json
import re
from typing import Any

from fastapi import HTTPException

Rows = list[dict[str, Any]]
Sheets = dict[str, Rows]

# Parsing Python-style input builds an AST, which needs far more memory than JSON.
MAX_LITERAL_CHARS = 1_000_000
_JS_LITERALS = {"true": True, "false": False, "null": None}
_INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _bad_request(detail: str) -> HTTPException:
	return HTTPException(status_code=400, detail=detail)


class _JsLiterals(ast.NodeTransformer):
	def visit_Name(self, node: ast.Name) -> ast.AST:
		if node.id in _JS_LITERALS:
			return ast.Constant(_JS_LITERALS[node.id])
		return node


def _parse_json_lines(text: str) -> list[Any] | None:
	"""Parse JSON Lines (one value per line). None if the first line isn't JSON on its own."""
	rows = []
	for number, line in enumerate(text.splitlines(), start=1):
		if not line.strip():
			continue
		try:
			rows.append(json.loads(line))
		except json.JSONDecodeError as e:
			if not rows:
				return None
			raise _bad_request(
				f"Invalid JSON Lines: line {number}, column {e.colno}: {e.msg}."
			) from e
	return rows


def _parse_text(text: str) -> Any:
	"""Parse JSON or JSON Lines, falling back to Python literals (single quotes, True/None)."""
	text = text.strip().lstrip("\ufeff")
	if not text:
		raise _bad_request("No data to export. The provided data is empty.")
	try:
		parsed = json.loads(text)
	except json.JSONDecodeError as json_error:
		if (
			json_error.msg == "Extra data"
			and (rows := _parse_json_lines(text)) is not None
		):
			return rows
		message = f"Invalid JSON: {json_error.msg} (line {json_error.lineno}, column {json_error.colno})."
		if len(text) > MAX_LITERAL_CHARS:
			raise _bad_request(message) from json_error
		# Any failure here just means the input isn't a Python literal either.
		try:
			tree = _JsLiterals().visit(ast.parse(text, mode="eval"))
			parsed = ast.literal_eval(tree)
		except Exception:
			raise _bad_request(message) from json_error
	# Double-encoded JSON: a JSON string whose content is the real JSON.
	if isinstance(parsed, str):
		try:
			parsed = json.loads(parsed)
		except json.JSONDecodeError:
			pass
	return parsed


def _sheet_name(name: str, taken: set[str]) -> str:
	"""Make a key a valid, unique Excel sheet name (max 31 chars, no []:*?/\\)."""
	base = _INVALID_SHEET_CHARS.sub("_", str(name)).strip("' ")[:31] or "Sheet"
	candidate, n = base, 1
	while candidate.lower() in taken:
		n += 1
		suffix = f" ({n})"
		candidate = base[: 31 - len(suffix)] + suffix
	taken.add(candidate.lower())
	return candidate


def validate_and_parse_data(data: str | list[Any] | dict[str, Any]) -> Sheets:
	"""Validate input and return it as {sheet name: rows}.

	- A list of objects gives one sheet named "Data".
	- An object whose values are all lists of objects gives one sheet per key.
	- Any other object is a single row.
	"""
	parsed = _parse_text(data) if isinstance(data, str) else data

	if isinstance(parsed, list):
		raw_sheets = {"Data": parsed}
	elif (
		isinstance(parsed, dict)
		and parsed
		and all(
			isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
			for rows in parsed.values()
		)
	):
		raw_sheets = parsed
	elif isinstance(parsed, dict):
		raw_sheets = {"Data": [parsed]}
	else:
		raise _bad_request(
			"Data must be a JSON array of objects, a single object, "
			"or an object of arrays (one sheet per key)."
		)

	for rows in raw_sheets.values():
		if not all(isinstance(row, dict) for row in rows):
			raise _bad_request("All items in the data list must be objects.")
	if not any(raw_sheets.values()):
		raise _bad_request("No data to export. The provided data is empty.")

	taken: set[str] = set()
	return {_sheet_name(name, taken): rows for name, rows in raw_sheets.items()}


def snake_to_pascal_case(name: str) -> str:
	"""Turn a key like "student_name", "studentName" or "address.city" into "Student Name"."""
	spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(name))
	words = [word for word in re.split(r"[\W_]+", spaced) if word]
	return " ".join(word[:1].upper() + word[1:] for word in words) or str(name)
