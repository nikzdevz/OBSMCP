@echo off
setlocal

if not exist "%USERPROFILE%\.obsmcp\config.json" (
    echo ================================================
    echo   OBSMCP First-Run Setup
    echo ================================================
    echo.
    set /p PROJECT_PATH="Enter project path (e.g. D:\Projects\MyProject): "
    echo.
    echo Optional: cloud sync configuration ^(leave blank for standalone mode^)
    echo.
    set /p BACKEND_URL="Enter backend URL (blank = standalone): "
    set /p API_TOKEN="Enter API token (blank = no auth): "
    echo.

    python -m obsmcp.obsmcp_setup --configure --project "%PROJECT_PATH%" --url "%BACKEND_URL%" --token "%API_TOKEN%"
    if errorlevel 1 (
        echo Configuration failed. Please check your inputs and try again.
        pause
        exit /b 1
    )
    echo.
    if "%BACKEND_URL%"=="" (
        echo Mode: STANDALONE ^(all data stored locally^)
    ) else (
        echo Mode: CLOUD SYNC ^(data syncing to %BACKEND_URL%^)
    )
    echo.
)

python -m obsmcp
