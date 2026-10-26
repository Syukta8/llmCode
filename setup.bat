@echo off
REM ==============================================================================
REM llmCode - Unified Installer & Setup Script (Windows 10/11)
REM Sets up Python venv, compiles/downloads backend engines in src\ & bin\,
REM installs CLI agents (Aider, OpenCode), and initializes config\config.json.
REM ==============================================================================
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

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

echo Installing core dashboard dependencies...
"%VENV_PY%" -m pip install --upgrade pip -q
"%VENV_PY%" -m pip install -q -r "%SCRIPT_DIR%src\LLMServer\requirements.txt"

echo Running backend engine and CLI agent setup...
"%VENV_PY%" "%SCRIPT_DIR%llmcode.py" setup %*

echo.
echo === llmCode Setup Complete ===
echo   Launch dashboard : launch.bat
echo   Stop services    : stop.bat
