@echo off
setlocal
set "OBSMCP_CALLER_CWD=%CD%"
if not defined OBSMCP_PROJECT set "OBSMCP_PROJECT=%OBSMCP_CALLER_CWD%"
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m cli.main %*
) else (
  py -3 -m cli.main %*
)
