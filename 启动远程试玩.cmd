@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "runtime\python\python.exe" (
  echo Portable Python was not found. Extract the complete game package first.
  pause
  exit /b 1
)
echo Sanwufan remote table
echo Keep this window open while friends are playing.
"runtime\python\python.exe" -X utf8 -m sanwufan.remote %*
set "SWF_EXIT=%ERRORLEVEL%"
if /i "%~1"=="--help" exit /b %SWF_EXIT%
if not "%SWF_EXIT%"=="0" pause
exit /b %SWF_EXIT%
