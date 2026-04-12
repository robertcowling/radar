@echo off
cd /d "%~dp0\.."
for /f "tokens=*" %%i in ('type tools\.env ^| findstr /v "^#"') do set %%i
echo Installing/checking dependencies...
"%LocalAppData%\Programs\Python\Python313\python.exe" -m pip install -q eccodes cfgrib xarray scipy matplotlib Pillow
"%LocalAppData%\Programs\Python\Python313\python.exe" fetch_mslp.py
pause
