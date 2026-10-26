@echo off
REM ==============================================================================
REM llmCode - Unified Windows Installer & Setup Script
REM ==============================================================================
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "VENV_DIR=%SCRIPT_DIR%src\LLMServer\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

REM Fast-start path if already setup
if "%1"=="run" goto :FAST_START
if "%1"=="dash" goto :FAST_START
goto :FULL_SETUP

:FAST_START
if exist "%VENV_PY%" (
    echo Starting llmCode dashboard...
    "%VENV_PY%" "%SCRIPT_DIR%llmcode.py" dash
    exit /b 0
)

:FULL_SETUP
echo === llmCode Setup (Windows) ===

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: python not found in PATH. Please install Python 3.10+ from python.org
    exit /b 1
)

if not exist "%VENV_DIR%" (
    echo Creating virtual environment at %VENV_DIR%...
    python -m venv "%VENV_DIR%"
)

echo Installing Python dependencies...
"%VENV_PY%" -m pip install --upgrade pip -q
"%VENV_PY%" -m pip install -q -r "%SCRIPT_DIR%src\LLMServer\requirements.txt"

if "%1"=="--with-tools" (
    echo Installing optional coding agents (aider-chat)...
    "%VENV_PY%" -m pip install -q aider-chat
)

echo Configuring engine binaries...
"%VENV_PY%" "%SCRIPT_DIR%llmcode.py" setup

echo === llmCode Setup Complete ===
echo Launch dashboard anytime with: setup.bat run (or: setup.bat dash)

if "%~1"=="run" (
    "%VENV_PY%" "%SCRIPT_DIR%llmcode.py" dash
)
if "%~1"=="dash" (
    "%VENV_PY%" "%SCRIPT_DIR%llmcode.py" dash
)
