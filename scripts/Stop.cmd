@echo off
setlocal
set "CCM_EXE=%~dp0Codex-Control-MCP.exe"
"%CCM_EXE%" stop --target https
if errorlevel 1 exit /b 1
"%CCM_EXE%" stop --target core
if errorlevel 1 (
    echo Run from an administrator terminal if Windows refuses the owned stop signal.
    exit /b 1
)
exit /b 0
