@echo off
title R2 Bucket Browser
echo Starting R2 Bucket Browser on http://localhost:2000 ...
echo.

REM Activate the venv if it exists alongside the repo
if exist "%~dp0..\venv\Scripts\activate.bat" (
    call "%~dp0..\venv\Scripts\activate.bat"
)

REM Launch the server
python "%~dp0r2_browser.py" 2000

pause
