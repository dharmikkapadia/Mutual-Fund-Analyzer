@echo off
setlocal
cd /d "%~dp0"
title AFP NAV Explorer

echo ============================================
echo    AFP NAV Explorer
echo ============================================
echo.

REM --- Check Python is available ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 'python' was not found on PATH.
    echo Install Python 3 and make sure 'python' runs in a terminal, then retry.
    echo.
    pause
    exit /b 1
)

REM --- Ensure dependencies (first run installs them) ---
python -c "import streamlit, customtkinter, plotly, pandas, numpy, requests" >nul 2>&1
if errorlevel 1 (
    echo First run: installing dependencies, please wait...
    echo.
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency install failed. See messages above.
        pause
        exit /b 1
    )
    echo.
)

REM --- Launch the desktop launcher (which starts Streamlit) ---
echo Starting AFP NAV Explorer...
echo Keep this window open while using the app; close it to stop the server.
echo.
python launcher.py

if errorlevel 1 (
    echo.
    echo [ERROR] The application exited with an error.
    pause
)
endlocal
