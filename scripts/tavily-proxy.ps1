#requires -Version 5.1
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "sync-keys")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path $PSScriptRoot -Parent
$ProxyRoot = Join-Path $ProjectRoot "vendor\tavily_search_sub_api"
$ProjectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path (Join-Path $ProxyRoot "app\service.py"))) {
    throw "Tavily proxy submodule is missing. Run: git submodule update --init --recursive"
}
if (-not (Test-Path $ProjectPython)) {
    throw "Project Python environment is missing: $ProjectPython"
}

function Sync-ProxyKeys {
    & $ProjectPython -m cli.tavily_proxy sync-keys `
        --target (Join-Path $ProxyRoot "key.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to synchronize Tavily proxy keys."
    }
}

switch ($Action) {
    "sync-keys" {
        Sync-ProxyKeys
    }
    "start" {
        Sync-ProxyKeys
        $env:HOST = "127.0.0.1"
        $env:PORT = "15280"
        $env:WORKERS = "1"
        $env:TAVILY_MAX_CONCURRENT_SEARCHES_PER_KEY = "1"
        $env:PYTHON_BIN = $ProjectPython
        & (Join-Path $ProxyRoot "scripts\start.bat")
        if ($LASTEXITCODE -ne 0) {
            throw "Tavily proxy failed to start."
        }
    }
    "stop" {
        & (Join-Path $ProxyRoot "scripts\stop.bat")
        if ($LASTEXITCODE -ne 0) {
            throw "Tavily proxy failed to stop cleanly."
        }
    }
    "status" {
        & (Join-Path $ProxyRoot "scripts\status.bat")
        exit $LASTEXITCODE
    }
}
