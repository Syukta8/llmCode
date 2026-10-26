@echo off
REM ==============================================================================
REM llmCode - Launch llm-dash Service (Windows)
REM Auto-verifies paths in config\config.json, starts llm-dash daemon in background,
REM and opens the browser.
REM ==============================================================================
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "VENV_PY=%SCRIPT_DIR%src\LLMServer\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Virtual environment missing. Running setup.bat first...
    call "%SCRIPT_DIR%setup.bat"
)

"%VENV_PY%" "%SCRIPT_DIR%llmcode.py" dash --daemon %*
