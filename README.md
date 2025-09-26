# JSON to Excel/CSV Converter

A FastAPI-based web application that converts JSON data into Excel (.xlsx) or CSV (.csv) format with a beautiful HTML interface.

## Features

- 🚀 **FastAPI Backend**: High-performance API with automatic documentation
- 📊 **Multiple Formats**: Export to Excel (.xlsx) or CSV (.csv)
- 🎨 **Beautiful UI**: Modern, responsive HTML interface
- 📝 **Flexible Input**: Accept JSON strings or arrays of objects
- 🔄 **Column Transformation**: Automatically converts snake_case to Pascal Case
- 📁 **File Download**: Direct download of generated files
- 🔍 **Data Validation**: Comprehensive input validation and error handling
- 📱 **Responsive Design**: Works on desktop and mobile devices

## Installation

1. **Clone the repository**:

   ```bash
   git clone <repository-url>
   cd fastapi-json-to-xlsx
   ```

2. **Install dependencies**:

   ```bash
   pip install -e .
   ```

3. **Run the application**:

   ```bash
   uvicorn main:app --reload
   ```

4. **Open your browser** and navigate to `http://localhost:8000`

## Usage

### Web Interface

1. Open `http://localhost:8000` in your browser
2. Paste your JSON data in the text area
3. Select export format (Excel or CSV)
4. Enter a filename (without extension)
5. Click "Export Data" to download the file

### API Endpoints

#### POST `/export`

Export data using JSON payload.

**Request Body**:

```json
{
  "data": [
    {
      "student_name": "John Doe",
      "course_title": "Mathematics 101",
      "registration_date": "2024-01-15",
      "grade": "A"
    }
  ],
  "format": "excel",
  "filename": "student_data"
}
```

**Response**: File download with appropriate headers.

#### POST `/export-raw`

Export data using raw request body (useful for large datasets).

**Query Parameters**:

- `format`: "excel" or "csv" (default: "excel")
- `filename`: Output filename without extension (default: "exported_data")

**Request Body**: Raw JSON string

**Example**:

```bash
curl -X POST "http://localhost:8000/export-raw?format=csv&filename=my_data" \
  -H "Content-Type: application/json" \
  -d '[{"name": "John", "age": 30}]'
```

## Example Data Format

The application expects JSON data as an array of objects:

```json
[
  {
    "student_name": "John Doe",
    "course_title": "Mathematics 101",
    "registration_date": "2024-01-15",
    "grade": "A",
    "credits": 3
  },
  {
    "student_name": "Jane Smith",
    "course_title": "Physics 201",
    "registration_date": "2024-01-20",
    "grade": "B+",
    "credits": 4
  }
]
```

## Column Name Transformation

The application automatically converts snake_case column names to Pascal Case with spaces:

- `student_name` → `Student Name`
- `course_title` → `Course Title`
- `registration_date` → `Registration Date`

## Error Handling

The application provides comprehensive error handling for:

- Invalid JSON format
- Empty data
- Non-array data structures
- Non-object items in arrays
- Server errors

## Testing

Run the test suite to verify functionality:

```bash
python test_converter.py
```

## API Documentation

FastAPI automatically generates interactive API documentation:

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

## Dependencies

- **FastAPI**: Web framework
- **Pandas**: Data manipulation
- **OpenPyXL**: Excel file generation
- **Jinja2**: HTML templating
- **Uvicorn**: ASGI server

## Development

The project structure:

```
fastapi-json-to-xlsx/
├── main.py                 # FastAPI application
├── templates/
│   └── index.html         # HTML interface
├── test_converter.py      # Test suite
├── pyproject.toml         # Dependencies
└── README.md              # This file
```

## License

This project is open source and available under the MIT License.
