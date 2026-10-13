$ErrorActionPreference = 'Stop'

$node = 'C:\Program Files\nodejs\node.exe'
$npm = 'C:\Program Files\nodejs\npm.cmd'
$launcher = 'C:\Users\mhj\Desktop\mhj_workspace\personal\orca_harness\tools\run-media-mock-server-isolated.mjs'
$cwd = (Get-Location).Path
$controlPort = 27100
$mediaPort = 27101
$zlmPort = 8898

function Test-PortOpen([string]$HostName, [int]$Port, [int]$TimeoutMs = 250) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if (-not $task.Wait($TimeoutMs)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

foreach ($port in @($controlPort, $mediaPort, $zlmPort)) {
    if (Test-PortOpen '127.0.0.1' $port 200) {
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
        if (Test-PortOpen '127.0.0.1' $controlPort 250) {
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
