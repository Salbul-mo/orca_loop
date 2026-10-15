$ErrorActionPreference = 'Stop'

$env:SystemRoot = 'C:\Windows'
$env:WINDIR = 'C:\Windows'
$env:COMSPEC = 'C:\Windows\System32\cmd.exe'
$env:PATHEXT = '.COM;.EXE;.BAT;.CMD'
$env:JAVA_HOME = 'C:\Users\mhj\.vscode\extensions\redhat.java-1.56.0-win32-x64\jre\21.0.12.1-win32-x86_64'
$env:PATH = "$env:JAVA_HOME\bin;C:\Windows\System32;C:\Windows;C:\Program Files\nodejs"
$env:TEMP = 'C:\Windows\Temp'
$env:TMP = 'C:\Windows\Temp'

$repo = (Get-Location).Path
$gradlew = Join-Path $repo 'gradlew.bat'
if (-not (Test-Path $gradlew)) {
    throw "gradlew.bat not found in $repo"
}

& $gradlew test --no-daemon
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$mockDir = Join-Path $repo 'mock\media-server'
Push-Location $mockDir
try {
    & 'C:\Program Files\PowerShell\7\pwsh.exe' -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\mhj\Desktop\mhj_workspace\personal\orca_harness\tools\run-media-mock-smoke.ps1'
    $smokeExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $smokeExit
