@echo off
rem topopaper-settings for Windows: opens the settings window (no console).
setlocal
set "TOPOPAPER_BIN=%~dp0"
set "PYTHONPATH=%~dp0..\share\topopaper"
set "PYTHONUTF8=1"
set "PYW=%LOCALAPPDATA%\topopaper\venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" -m topopaper.settings %*
