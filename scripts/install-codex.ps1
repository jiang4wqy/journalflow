$ErrorActionPreference = 'Stop'
$journalflowRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$journalflowVenv = Join-Path $journalflowRoot '.venv'
$journalflowPython = Join-Path $journalflowVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $journalflowPython)) {
    py -3.12 -m venv $journalflowVenv
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
& $journalflowPython -m pip install "${journalflowRoot}[mcp]"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
$journalflow = Join-Path $journalflowVenv 'Scripts\journalflow.exe'
$journalflowConfig = Join-Path $journalflowRoot 'journalflow.toml'
if (-not (Test-Path -LiteralPath $journalflowConfig)) {
    & $journalflow --config $journalflowConfig init
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
Write-Output 'JournalFlow and MCP are installed. Import a PDF and run index-text before querying it.'
Write-Output "codex mcp add journalflow -- `"$journalflow`" --config `"$journalflowConfig`" mcp"
