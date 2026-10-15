@echo off
rem Manual equivalent of the scheduled task "CareerOS daily run" (which starts pythonw.exe directly, with no window).
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m careeros run --output-file "..\vault\logs\daily-run.log"
