#!/bin/bash
set -e
cd -- "$(dirname -- "$0")/.."
journalflowProject="$PWD"
export PYTHONPATH="$journalflowProject/src"
case "$(uname -m)" in
  arm64) journalflowTarget=macos-arm64 ;;
  x86_64) journalflowTarget=macos-x64 ;;
  *) echo '支持 macOS Apple Silicon 或 Intel。'; exit 1 ;;
esac
journalflowPython="$journalflowProject/runtime/$journalflowTarget/python/bin/python3"
if [[ ! -x "$journalflowPython" ]]; then
  journalflowPython="$journalflowProject/.venv/bin/python"
  if [[ ! -x "$journalflowPython" ]]; then
    python3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
    python3 -m venv "$journalflowProject/.venv"
  fi
fi
if ! "$journalflowPython" -c 'import journalflow.desktop, pypdfium2, filelock, httpx, numpy, onnxruntime, tokenizers; from mcp.server.fastmcp import FastMCP' >/dev/null 2>&1; then
  echo '首次安装需要网络：正在安装本项目的 MCP 和区域索引依赖。'
  "$journalflowPython" -m pip install --disable-pip-version-check -e "$journalflowProject[mcp,regional]"
fi
if [[ ! -f "$journalflowProject/journalflow.toml" ]]; then
  "$journalflowPython" -m journalflow --config "$journalflowProject/journalflow.toml" init
fi
exec "$journalflowPython" -m journalflow --config "$journalflowProject/journalflow.toml" desktop
