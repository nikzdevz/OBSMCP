@echo off
setlocal
cd /d "%~dp0"

if exist "scripts\launch_obsmcp.vbs" (
  wscript.exe //nologo "scripts\launch_obsmcp.vbs"
  timeout /t 3 /nobreak >nul
  powershell -NoProfile -Command "$client = New-Object System.Net.Sockets.TcpClient; try { $client.Connect('127.0.0.1', 9300); exit 0 } catch { exit 1 } finally { $client.Dispose() }"
  if %ERRORLEVEL% EQU 0 (
    echo obsmcp started on http://127.0.0.1:9300
    exit /b 0
  )
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\launch_obsmcp.py
) else (
  py -3 scripts\launch_obsmcp.py
)
