@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0.." || exit /b 1
set "journalflowProject=%CD%"
set "PYTHONPATH=%journalflowProject%\src"
set "journalflowPython=%journalflowProject%\runtime\windows-x64\python\python.exe"
if exist "%journalflowPython%" goto runtime_ready
set "journalflowPython=%journalflowProject%\.venv\Scripts\python.exe"
if exist "%journalflowPython%" goto runtime_ready
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
if errorlevel 1 goto failed
py -3 -m venv "%journalflowProject%\.venv"
if errorlevel 1 goto failed
goto runtime_ready
:use_python
python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
if errorlevel 1 goto failed
python -m venv "%journalflowProject%\.venv"
if errorlevel 1 goto failed
:runtime_ready
"%journalflowPython%" -c "import journalflow.desktop, pypdfium2, filelock, httpx, numpy, onnxruntime, tokenizers; from mcp.server.fastmcp import FastMCP" >nul 2>nul
if not errorlevel 1 goto configured
echo 首次安装需要网络：正在安装本项目的 MCP 和区域索引依赖。
"%journalflowPython%" -m pip install --disable-pip-version-check -e "%journalflowProject%[mcp,regional]"
if errorlevel 1 goto failed
:configured
if exist "%journalflowProject%\journalflow.toml" goto launch
"%journalflowPython%" -m journalflow --config "%journalflowProject%\journalflow.toml" init
if errorlevel 1 goto failed
:launch
"%journalflowPython%" -m journalflow --config "%journalflowProject%\journalflow.toml" desktop
if errorlevel 1 goto failed
popd
exit /b 0
:failed
echo 启动失败。请查看上方信息；需要 Python 3.11+，首次安装需要可用网络。
pause
popd
exit /b 1
