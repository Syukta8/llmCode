@echo off
REM ==============================================================================
REM llmCode - Launch Dashboard (Windows)
REM ==============================================================================
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."
cd /d "%REPO_ROOT%"

set "VENV_PY=%REPO_ROOT%\src\LLMServer\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Virtual environment missing. Running setup first...
    call "%REPO_ROOT%\setup.bat"
)

echo Starting llmCode Control Center...
"%VENV_PY%" "%REPO_ROOT%\llmcode.py" dash
