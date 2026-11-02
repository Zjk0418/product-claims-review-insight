@echo off
setlocal
cd /d "%~dp0"
set "REVIEW_APP_PATH=dist\ReviewInsight\ReviewInsight.exe"
if exist "%~dp0dist\current-app.txt" set /p REVIEW_APP_PATH=<"%~dp0dist\current-app.txt"
set "REVIEW_APP_PATH=%REVIEW_APP_PATH:/=\%"
if exist "%~dp0%REVIEW_APP_PATH%" (
  start "" "%~dp0%REVIEW_APP_PATH%"
  exit /b
)
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0desktop_launcher.py"
  exit /b
)
echo Application files were not found. Please extract the complete portable package.
pause
