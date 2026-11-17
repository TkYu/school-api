@echo off
rem Crawl the school's official site until this window is closed or Ctrl+C is pressed.
rem Data: data\site.sqlite (continues where it stopped), log: logs\crawl.log
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
title School site crawler
if not exist ".venv\Scripts\python.exe" (
  echo The Python environment is missing. Run setup.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" crawl.py %*
echo.
echo The crawler has stopped.
pause
