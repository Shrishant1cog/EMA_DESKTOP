@echo off
setlocal enabledelayedexpansion
title EMA Dependency Installer

cd /d "%~dp0"
set "LOG_FILE=%~dp0install_log.txt"
echo ====================================================== > "%LOG_FILE%"
echo   EMA Environment Installation Log                     >> "%LOG_FILE%"
echo   Started at: %DATE% %TIME%                            >> "%LOG_FILE%"
echo ====================================================== >> "%LOG_FILE%"

:: 1. Locate working Python runtime (Bypass WindowsApps 0-byte stub)
set "SYSTEM_PY="

py -3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set "SYSTEM_PY=py -3"
    goto :PYTHON_FOUND
)

python --version >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('where python') do (
        echo "%%i" | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            set "SYSTEM_PY="%%i""
            goto :PYTHON_FOUND
        )
    )
)

echo [!] Error: No valid Python 3.10+ runtime found on this system. >> "%LOG_FILE%"
exit /b 1

:PYTHON_FOUND
echo [*] Detected Python: %SYSTEM_PY% >> "%LOG_FILE%"

:: 2. Create isolated virtual environment if missing
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [*] Creating .venv virtual environment... >> "%LOG_FILE%"
    %SYSTEM_PY% -m venv "%~dp0.venv" >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [X] Failed to initialize virtual environment. >> "%LOG_FILE%"
        exit /b 1
    )
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

:: 3. Upgrade pip and core packaging tools
echo [*] Upgrading pip and wheel... >> "%LOG_FILE%"
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel --no-warn-script-location >> "%LOG_FILE%" 2>&1

:: 4. Install requirements using pre-compiled binary wheels
echo [*] Installing requirements from requirements.txt... >> "%LOG_FILE%"
"%VENV_PY%" -m pip install --prefer-binary -r "%~dp0requirements.txt" --no-warn-script-location >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [X] Pip installation encountered errors. Check log above. >> "%LOG_FILE%"
    exit /b 1
)

:: 5. Verification Check: Ensure uvicorn, fastapi, and webview import cleanly
echo [*] Verifying critical modules... >> "%LOG_FILE%"
"%VENV_PY%" -c "import uvicorn, fastapi, pydantic, groq, googleapiclient; print('Verification: ALL CORE MODULES IMPORTED OK')" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [X] Module integrity check failed. >> "%LOG_FILE%"
    exit /b 1
)

echo [✔] Installation completed successfully at: %DATE% %TIME% >> "%LOG_FILE%"
exit /b 0