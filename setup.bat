@echo off
REM ==============================================================================
REM llmCode - Unified Installer, Hardware Scanner & Uninstaller (Windows 10/11)
REM Supports:
REM   setup.bat              -> Full interactive setup (hardware.json, engines, venv agents, Continue)
REM   setup.bat --uninstall  -> Complete uninstall of venv, engines, binaries, models, configs
REM ==============================================================================
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM Check for --uninstall before touching .venv (avoids Windows file lock on .venv\Scripts\python.exe)
set "IS_FULL=0"
for %%A in (%*) do (
    if /i "%%~A"=="--uninstall" (
        python "%SCRIPT_DIR%llmcode.py" setup %*
        exit /b 0
    )
    if /i "%%~A"=="--full" (
        set "IS_FULL=1"
    )
)

set "VENV_DIR=%SCRIPT_DIR%src\LLMServer\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo === llmCode Full Setup (Windows) ===

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [MISSING] Python 3.10+ not found in PATH.
    set /p "AUTO_PY=Permit auto-install of Python 3.12 via winget? [y/N]: "
    if /i "!AUTO_PY!"=="y" (
        winget install -e --id Python.Python.3.12 --source winget
    ) else (
        echo Manual guidance: Install Python 3.10+ from https://www.python.org/downloads/ and check "Add Python to PATH".
        exit /b 1
    )
)

if not exist "%VENV_PY%" (
    echo Creating virtual environment at %VENV_DIR%...
    python -m venv "%VENV_DIR%"
)

if "%IS_FULL%"=="0" if exist "%SCRIPT_DIR%config\deploy_state.json" (
    echo Existing deployment detected. Running incremental update check...
    "%VENV_PY%" "%SCRIPT_DIR%llmcode.py" check-update %*
    exit /b %errorlevel%
)

echo Installing core dashboard dependencies...
"%VENV_PY%" -m pip install --upgrade pip -q
"%VENV_PY%" -m pip install -q -r "%SCRIPT_DIR%src\LLMServer\requirements.txt"

echo Running hardware scanner, backend engine setup, and CLI agent installer...
"%VENV_PY%" "%SCRIPT_DIR%llmcode.py" setup %*

echo.
echo === llmCode Setup Complete ===
echo   Launch dashboard : launch.bat
echo   Stop services    : stop.bat
echo   Uninstall all    : setup.bat --uninstall
