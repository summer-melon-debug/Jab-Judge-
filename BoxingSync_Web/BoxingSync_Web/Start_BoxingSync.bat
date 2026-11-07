@echo off
setlocal
cd /d "%~dp0"
where ffmpeg >nul 2>nul
if errorlevel 1 goto ffmpeg_missing
where ffprobe >nul 2>nul
if errorlevel 1 goto ffmpeg_missing
if not exist ".venv\Scripts\python.exe" (
    python -m venv --without-pip .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m ensurepip --upgrade
    if errorlevel 1 goto pip_missing
    if exist ".venv\boxingsync-ready" del ".venv\boxingsync-ready"
)
if not exist ".venv\boxingsync-ready" (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
    type nul > ".venv\boxingsync-ready"
)
".venv\Scripts\python.exe" app.py --open
if errorlevel 1 goto failed
exit /b 0

:ffmpeg_missing
echo FFmpeg and FFprobe must be on PATH. See README.md.
pause
exit /b 1

:pip_missing
echo Python could not install pip in the virtual environment. See the Conda instructions in README.md.
pause
exit /b 1

:failed
echo BoxingSync could not start. Check the message above and see README.md.
pause
exit /b 1
