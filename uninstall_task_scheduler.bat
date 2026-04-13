@echo off
setlocal
set "TASK_NAME=obsmcp"

SCHTASKS /Delete /TN "%TASK_NAME%" /F
if %ERRORLEVEL% EQU 0 (
  echo Removed Task Scheduler entry "%TASK_NAME%".
) else (
  echo Failed to remove Task Scheduler entry "%TASK_NAME%".
  exit /b 1
)
