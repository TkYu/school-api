@echo off
rem A consistent copy of the database, safe while the crawler runs: data\site-YYYYMMDD-HHMM.sqlite
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmm"') do set STAMP=%%t
".venv\Scripts\python.exe" crawl.py --snapshot "data\site-%STAMP%.sqlite"
pause
