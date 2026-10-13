# PowerShell 5.1 / 7 compatible; run in your own logged-in desktop session.
# Does not print, persist, or transmit the control secret outside loopback.
param([switch]$General, [switch]$Format, [switch]$ReadTool, [switch]$MultiRead, [switch]$InspectRead)
$selectedModes = @($General, $Format, $ReadTool, $MultiRead, $InspectRead) | Where-Object { $_ }
if (@($selectedModes).Count -gt 1) {
    throw 'Choose at most one probe mode: -General, -Format, -ReadTool, -MultiRead, or -InspectRead.'
}
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$remote = Join-Path (Split-Path -Parent $root) 'remote-mcp'
$envPath = Join-Path $remote '.env'
$python = Join-Path $root '.venv\Scripts\python.exe'
$adapter = Join-Path $root 'tools\run_web_gpt_loop.py'
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw 'remote-mcp .env not found'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'orca_harness .venv Python not found'
}
if (-not (Test-Path -LiteralPath $adapter -PathType Leaf)) {
    throw 'web GPT launcher not found'
}
$matches = @(Get-Content -LiteralPath $envPath | Where-Object { $_ -match '^MCP_WEB_AGENT_CONTROL_TOKEN=' })
if ($matches.Count -ne 1) { throw 'Expected exactly one MCP_WEB_AGENT_CONTROL_TOKEN entry in .env' }
$secret = ($matches[0] -split '=', 2)[1].Trim().Trim('"', "'")
if ($secret.Length -lt 32) { throw 'Web agent control secret is missing or too short' }
$oldToken = [Environment]::GetEnvironmentVariable('ORCA_WEB_AGENT_TOKEN', 'Process')
$oldBridge = [Environment]::GetEnvironmentVariable('ORCA_WEB_AGENT_BRIDGE_URL', 'Process')
$oldBytecode = [Environment]::GetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', 'Process')
try {
    $hello = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/hello' -Headers @{ 'x-extension-protocol' = '9' } -TimeoutSec 5
    # Transitional compatibility while Windows service and Chrome extension
    # move from the retired bridge identity to the remote-mcp name.
    if ($hello.app -notin @('remote-mcp', 'cokacremote-bridge') -or $hello.compatible -ne $true) {
        throw 'remote-mcp bridge did not return the expected compatible protocol'
    }
    Write-Output 'BRIDGE=PASS'
    $env:ORCA_WEB_AGENT_BRIDGE_URL = 'http://127.0.0.1:8765'
    $env:ORCA_WEB_AGENT_TOKEN = $secret
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $secret = $null
    & $python $adapter --check-config
    if ($LASTEXITCODE -ne 0) { throw 'Web GPT model provider configuration check failed' }
    if ($InspectRead) {
        Write-Output 'LIVE_INSPECT_READ_BROWSER_PROBE=STARTED'
        & $python $adapter --probe-inspect-read-tool
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            Write-Output ('LIVE_INSPECT_READ_BROWSER_PROBE=FAILED exit=' + $code)
            exit $code
        }
        Write-Output 'LIVE_INSPECT_READ_BROWSER_PROBE=PASS'
    } elseif ($MultiRead) {
        Write-Output 'LIVE_MULTI_READ_BROWSER_PROBE=STARTED'
        & $python $adapter --probe-multi-read-tool
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            Write-Output ('LIVE_MULTI_READ_BROWSER_PROBE=FAILED exit=' + $code)
            exit $code
        }
        Write-Output 'LIVE_MULTI_READ_BROWSER_PROBE=PASS'
    } elseif ($ReadTool) {
        Write-Output 'LIVE_READ_TOOL_BROWSER_PROBE=STARTED'
        & $python $adapter --probe-read-tool
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            Write-Output ('LIVE_READ_TOOL_BROWSER_PROBE=FAILED exit=' + $code)
            exit $code
        }
        Write-Output 'LIVE_READ_TOOL_BROWSER_PROBE=PASS'
    } elseif ($Format) {
        Write-Output 'LIVE_FORMAT_BROWSER_PROBE=STARTED'
        & $python $adapter --probe-format
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            Write-Output ('LIVE_FORMAT_BROWSER_PROBE=FAILED exit=' + $code)
            exit $code
        }
        Write-Output 'LIVE_FORMAT_BROWSER_PROBE=PASS'
    } elseif ($General) {
        Write-Output 'LIVE_GENERAL_BROWSER_PROBE=STARTED'
        & $python $adapter --probe-general
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            Write-Output ('LIVE_GENERAL_BROWSER_PROBE=FAILED exit=' + $code)
            exit $code
        }
        Write-Output 'LIVE_GENERAL_BROWSER_PROBE=PASS'
    } else {
        Write-Output 'LIVE_BROWSER_PROBE=STARTED'
        & $python $adapter --probe
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            Write-Output ('LIVE_BROWSER_PROBE=FAILED exit=' + $code)
            exit $code
        }
        Write-Output 'LIVE_BROWSER_PROBE=PASS'
    }
    exit 0
} finally {
    [Environment]::SetEnvironmentVariable('ORCA_WEB_AGENT_TOKEN', $oldToken, 'Process')
    [Environment]::SetEnvironmentVariable('ORCA_WEB_AGENT_BRIDGE_URL', $oldBridge, 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', $oldBytecode, 'Process')
}
