@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo Sanwufan local table
echo Keep this window open. Press Ctrl+C to stop.
if exist "runtime\python\python.exe" (
  "runtime\python\python.exe" -X utf8 -m sanwufan.local %*
) else (
  python -X utf8 -m sanwufan.local %*
)
set "SWF_EXIT=%ERRORLEVEL%"
if /i "%~1"=="--help" exit /b %SWF_EXIT%
if not "%SWF_EXIT%"=="0" pause
exit /b %SWF_EXIT%
