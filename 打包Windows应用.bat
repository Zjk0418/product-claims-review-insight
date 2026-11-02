@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 tools\build_desktop.py
if errorlevel 1 (
  echo 打包或验收未通过，请查看输出。
) else (
  echo 已生成 dist\ReviewInsight\ReviewInsight.exe
)
pause
