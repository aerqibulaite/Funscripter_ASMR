@echo off
cd /d "%~dp0"
python tools\gui.py
if errorlevel 1 pause
