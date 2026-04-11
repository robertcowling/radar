@echo off
cd /d "%~dp0\.."
for /f "tokens=*" %%i in ('type tools\.env ^| findstr /v "^#"') do set %%i
"%LocalAppData%\Programs\Python\Python313\python.exe" make_accum_24hr.py
pause
