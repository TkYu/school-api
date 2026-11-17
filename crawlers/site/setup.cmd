@echo off
rem Create or update the Python environment (.venv) with uv.
cd /d "%~dp0"
uv sync --frozen
pause
