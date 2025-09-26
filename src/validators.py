"""
Data validation and parsing utilities for JSON to Excel/CSV converter.
"""

import ast
import json
from typing import Any

from fastapi import HTTPException


def validate_and_parse_data(
	data: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
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
					except (json.JSONDecodeError, TypeError) as e:
						raise HTTPException(
							status_code=400,
							detail="Invalid data format. Please use valid JSON format with double quotes, Python dictionary format, or stringified JSON.",
						) from e

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
