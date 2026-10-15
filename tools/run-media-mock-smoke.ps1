$ErrorActionPreference = 'Stop'

# ORCA test runner intentionally sanitizes the child environment. Recreate only
# the Windows process environment required by PowerShell/Node crypto startup.
$env:SystemRoot = 'C:\Windows'
$env:WINDIR = 'C:\Windows'
$env:COMSPEC = 'C:\Windows\System32\cmd.exe'
$env:PATHEXT = '.COM;.EXE;.BAT;.CMD'
$env:PATH = 'C:\Windows\System32;C:\Windows;C:\Program Files\nodejs'
$env:TEMP = 'C:\Windows\Temp'
$env:TMP = 'C:\Windows\Temp'

$node = 'C:\Program Files\nodejs\node.exe'
$launcher = 'C:\Users\mhj\Desktop\mhj_workspace\personal\orca_harness\tools\run-media-mock-server-isolated.mjs'
$netstat = 'C:\Windows\System32\netstat.exe'
$cwd = (Get-Location).Path
$controlPort = 27100
$mediaPort = 27101
$zlmPort = 8898

function Test-PortOpen([int]$Port) {
    if (-not (Test-Path $netstat)) {
        throw "netstat.exe not found at $netstat"
    }
    $pattern = ('^\s*TCP\s+\S+:' + $Port + '\s+\S+\s+LISTENING\s+\d+\s*$')
    return [bool](& $netstat -ano -p tcp | Select-String -Pattern $pattern -Quiet)
}

foreach ($port in @($controlPort, $mediaPort, $zlmPort)) {
    if (Test-PortOpen $port) {
        throw "Test port $port is already in use; refusing to test against an unknown process."
    }
}

$server = Start-Process -FilePath $node -ArgumentList $launcher -WorkingDirectory $cwd -PassThru -NoNewWindow

$exitCode = 1
try {
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        if ($server.HasExited) {
            throw "Mock server exited before readiness with code $($server.ExitCode)."
        }
        if (Test-PortOpen $controlPort) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) {
        throw "Mock server did not open control port $controlPort."
    }

    & $node smoke.js "ws://127.0.0.1:$controlPort/ws"
    $exitCode = $LASTEXITCODE
} finally {
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        $server.WaitForExit()
    }
}

exit $exitCode
