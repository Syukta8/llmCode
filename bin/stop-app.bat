@echo off
REM ==============================================================================
REM llmCode - Stop Dashboard & Servers (Windows)
REM ==============================================================================
setlocal enabledelayedexpansion

echo Stopping llmCode server processes...
taskkill /F /IM llama-server.exe >nul 2>&1
taskkill /F /IM ollama.exe >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq llmCode*" >nul 2>&1

echo Done.
