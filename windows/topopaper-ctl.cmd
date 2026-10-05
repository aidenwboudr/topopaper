@echo off
rem topopaper-ctl for Windows: every command (fly, search, doctor, ...).
rem Runs the Python package beside this install with the venv install.ps1 made.
setlocal
set "TOPOPAPER_BIN=%~dp0"
set "PYTHONPATH=%~dp0..\share\topopaper"
set "PYTHONUTF8=1"
set "PY=%LOCALAPPDATA%\topopaper\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m topopaper.cli %*
