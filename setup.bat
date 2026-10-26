@echo off
REM ==============================================================================
REM llmCode - Unified Windows Installer & Setup Script
REM ==============================================================================
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

echo === llmCode Setup (Windows) ===

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: python not found in PATH. Please install Python 3.10+ from python.org
    exit /b 1
)

set "VENV_DIR=%SCRIPT_DIR%src\LLMServer\.venv"
if not exist "%VENV_DIR%" (
    echo Creating virtual environment at %VENV_DIR%...
    python -m venv "%VENV_DIR%"
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

echo Installing Python dependencies...
"%VENV_PIP%" install --upgrade pip -q
"%VENV_PIP%" install -q -r "%SCRIPT_DIR%src\LLMServer\requirements.txt"

if "%1"=="--with-tools" (
    echo Installing optional coding agents (aider-chat)...
    "%VENV_PIP%" install -q aider-chat
)

echo Configuring engine binaries...
"%VENV_PY%" "%SCRIPT_DIR%llmcode.py" setup %*

echo === llmCode Setup Complete ===
echo Launch dashboard with: setup.bat run (or: %VENV_PY% llmcode.py dash)

if "%1"=="run" (
    "%VENV_PY%" "%SCRIPT_DIR%llmcode.py" dash
)
if "%1"=="dash" (
    "%VENV_PY%" "%SCRIPT_DIR%llmcode.py" dash
)
