@echo off
setlocal
set "TASK_NAME=obsmcp"
set "OBSPROJ=%~dp0"
set "BAT_FILE=%OBSPROJ%run_obsmcp.bat"

REM Remove existing task if present
SCHTASKS /Delete /TN "%TASK_NAME%" /F >nul 2>&1

REM Create scheduled task: runs at logon with 30-second delay
SCHTASKS /Create /TN "%TASK_NAME%" /SC ONLOGON /DELAY 0000:30 /RL LIMITED /TR "\"%BAT_FILE%\"" /F
if %ERRORLEVEL% EQU 0 (
    echo.
    echo obsmcp scheduled task installed: "%TASK_NAME%"
    echo Will start automatically 30 seconds after each logon.
    echo.
    SCHTASKS /Query /TN "%TASK_NAME%" /FO LIST
) else (
    echo.
    echo ERROR: Failed to create scheduled task.
    echo Run as Administrator if this fails.
    echo.
    exit /b 1
)
