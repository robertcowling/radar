@echo off
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 "') do (
    taskkill /f /pid %%a >nul 2>&1
)
start "" "http://localhost:8000?mslp=1"
"%LocalAppData%\Programs\Python\Python313\python.exe" -m http.server 8000 --directory "%~dp0\.."
