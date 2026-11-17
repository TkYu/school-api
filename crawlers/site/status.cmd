@echo off
rem Counts so far (safe while the crawler runs).
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
".venv\Scripts\python.exe" crawl.py --status
pause
