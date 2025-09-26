"""
Main FastAPI application entry point for JSON to Excel/CSV converter.
"""

from fastapi import FastAPI

from src.routes import router

app = FastAPI(title="JSON to Excel/CSV Converter")

app.include_router(router)
