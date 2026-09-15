@echo off
setlocal
set "CCM_EXE=%~dp0Codex-Control-MCP.exe"
if not exist "%CCM_EXE%" (
  echo Codex-Control-MCP.exe is missing beside this launcher.
  exit /b 1
)
call "%~dp0Start-Admin.cmd"
if errorlevel 1 exit /b 1
"%CCM_EXE%" connect --copy
if errorlevel 1 exit /b 1
echo.
echo In ChatGPT Developer mode, create an OAuth MCP connection:
echo https://codex-control.aiwsb.site/mcp
echo Leave OAuth client ID and secret empty for dynamic registration.
echo Paste the one-time clipboard code ONLY into this server's HTTPS consent page.
echo Code expires in five minutes and is single use. Do not paste it into chat.
pause
