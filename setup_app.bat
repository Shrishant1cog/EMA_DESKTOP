@echo off
setlocal enabledelayedexpansion
title Smart Universal Email Assistant - Build & Setup

echo ======================================================================
echo   SMART UNIVERSAL EMAIL ASSISTANT - BUILD ^& SHORTCUT SETUP
echo ======================================================================
echo.

:: 1. Locate Python runtime (Prefer project .venv)
set "PY_EXE="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
    echo [*] Using project virtual environment: .venv
) else (
    where python >nul 2>nul
    if %errorlevel% equ 0 (
        set "PY_EXE=python"
        echo [*] Using system Python runtime.
    ) else (
        echo [!] Error: Python runtime not found. Please install Python or set up .venv.
        pause
        exit /b 1
    )
)

:: 2. Ensure PyInstaller is installed
echo [*] Checking PyInstaller...
"%PY_EXE%" -m pip install pyinstaller --quiet

:: 3. Clean previous build caches
echo [*] Cleaning stale build artifacts...
if exist "%~dp0build" rd /s /q "%~dp0build"
if exist "%~dp0dist" rd /s /q "%~dp0dist"

:: 4. Compile standalone executable
echo.
echo [*] Compiling executable with assets (frontend, config, services)...
"%PY_EXE%" -m PyInstaller ^
    --noconsole ^
    --onefile ^
    --name "Smart Universal Email Assistant" ^
    --add-data "frontend;frontend" ^
    --add-data "config.py;." ^
    --clean ^
    "%~dp0desktop_app.py"

:: 5. Verify compilation
set "TARGET_EXE=%~dp0dist\Smart Universal Email Assistant.exe"
if not exist "%TARGET_EXE%" (
    echo.
    echo [X] Build failed: Executable was not found in dist\.
    pause
    exit /b 1
)

echo.
echo ======================================================================
echo   BUILD COMPLETED SUCCESSFULLY!
echo   Output: %TARGET_EXE%
echo ======================================================================
echo.

:: 6. Create VBScript prompt to ask for Desktop Shortcut
set "PROMPT_VBS=%temp%\prompt_shortcut_%random%.vbs"
(
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo ans = MsgBox^("Build completed successfully!" ^& vbCrLf ^& vbCrLf ^& "Would you like to create a shortcut on your Desktop?", 36, "Smart Universal Email Assistant Setup"^)
    echo If ans = 6 Then
    echo     desktopPath = WshShell.SpecialFolders^("Desktop"^)
    echo     Set shortcut = WshShell.CreateShortcut^(desktopPath ^& "\Smart Universal Email Assistant.lnk"^)
    echo     shortcut.TargetPath = "%TARGET_EXE:\=\\%"
    echo     shortcut.WorkingDirectory = "%~dp0dist"
    echo     shortcut.Description = "Smart Universal Email Assistant"
    echo     shortcut.Save
    echo     MsgBox "Desktop shortcut created successfully!", 64, "Setup Complete"
    echo End If
) > "%PROMPT_VBS%"

cscript //nologo "%PROMPT_VBS%"
del /f /q "%PROMPT_VBS%" 2>nul

echo [*] Setup process finished.
exit /b 0