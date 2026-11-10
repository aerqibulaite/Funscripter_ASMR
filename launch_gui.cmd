@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\gui.py
) else (
  python tools\gui.py
)
if errorlevel 1 pause
