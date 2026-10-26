@echo off
REM ==============================================================================
REM llmCode - Stop llm-dash & Inference Engine Services (Windows)
REM ==============================================================================
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "VENV_PY=%SCRIPT_DIR%src\LLMServer\.venv\Scripts\python.exe"

echo === Stopping llmCode Services ===
if exist "%VENV_PY%" (
    "%VENV_PY%" "%SCRIPT_DIR%llmcode.py" stop
) else (
    python "%SCRIPT_DIR%llmcode.py" stop 2>nul
)

taskkill /F /IM llama-server.exe >nul 2>&1
taskkill /F /IM koboldcpp.exe >nul 2>&1
if exist "%SCRIPT_DIR%config\llmserver.pid" del /f /q "%SCRIPT_DIR%config\llmserver.pid" >nul 2>&1
echo All llmCode services stopped.
