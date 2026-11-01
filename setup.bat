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
set "PASSTHROUGH_ARGS="
for %%A in (%*) do (
    if /i "%%~A"=="--uninstall" (
        python "%SCRIPT_DIR%llmcode.py" setup %*
        exit /b 0
    ) else if /i "%%~A"=="--full" (
        set "IS_FULL=1"
    ) else (
        set "PASSTHROUGH_ARGS=!PASSTHROUGH_ARGS! %%~A"
    )
)

set "VENV_DIR=%SCRIPT_DIR%src\LLMServer\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo === llmCode Full Setup (Windows) ===

set "BASE_PY="
py -3.12 --version >nul 2>&1 && set "BASE_PY=py -3.12"
if not defined BASE_PY py -3.11 --version >nul 2>&1 && set "BASE_PY=py -3.11"
if not defined BASE_PY py -3.10 --version >nul 2>&1 && set "BASE_PY=py -3.10"
if not defined BASE_PY python -c "import sys; sys.exit(0 if sys.version_info[0] == 3 and sys.version_info[1] in (10, 11, 12, 13) else 1)" >nul 2>&1 && set "BASE_PY=python"

if not defined BASE_PY (
    echo [MISSING] Python 3.10 to 3.12 not found in PATH.
    set /p "AUTO_PY=Permit auto-install of Python 3.12 via winget? [y/N]: "
    if /i "!AUTO_PY!"=="y" (
        winget install -e --id "Python.Python.3.12" --source winget
        set "BASE_PY=py -3.12"
    ) else (
        echo Manual guidance: Install Python 3.12 from https://www.python.org/downloads/ and check "Add Python to PATH".
        exit /b 1
    )
)

if not exist "%VENV_PY%" (
    echo Creating virtual environment at %VENV_DIR% using !BASE_PY!...
    !BASE_PY! -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to create Python virtual environment at %VENV_DIR%.
        exit /b !errorlevel!
    )
)

if "%IS_FULL%"=="0" if exist "%SCRIPT_DIR%config\deploy_state.json" (
    echo Existing deployment detected. Running incremental update check...
    "%VENV_PY%" "%SCRIPT_DIR%llmcode.py" check-update !PASSTHROUGH_ARGS!
    exit /b !errorlevel!
)

echo Installing core dashboard dependencies...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel -q --no-warn-script-location
if !errorlevel! neq 0 (
    echo [ERROR] Failed to upgrade pip in virtual environment.
    exit /b !errorlevel!
)

"%VENV_PY%" -m pip install -q --no-warn-script-location -r "%SCRIPT_DIR%src\LLMServer\requirements.txt"
if !errorlevel! neq 0 (
    echo [ERROR] Failed to install core dashboard dependencies.
    exit /b !errorlevel!
)

echo Running hardware scanner, backend engine setup, and CLI agent installer...
"%VENV_PY%" "%SCRIPT_DIR%llmcode.py" setup !PASSTHROUGH_ARGS!
if !errorlevel! neq 0 (
    echo [ERROR] llmCode setup failed.
    exit /b !errorlevel!
)

echo.
echo === llmCode Setup Complete ===
echo   Launch dashboard : launch.bat
echo   Stop services    : stop.bat
echo   Uninstall all    : setup.bat --uninstall
