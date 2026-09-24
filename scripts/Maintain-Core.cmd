@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0.."
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo 错误：未找到项目 Python，请先完成安装。
  exit /b 2
)
pushd "%ROOT%" || exit /b 2
"%PYTHON%" -X utf8 -m codex_control_mcp.maintenance %*
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
