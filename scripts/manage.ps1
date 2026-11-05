param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Project Python environment is missing. Follow the installation instructions in README.md.'
}
$runtimeJson = & $pythonPath '-B' '-c' 'import json,os,sys; os.chdir(sys.argv[1]); from app.config import load_settings; print(json.dumps(str(load_settings().database_path.resolve().parent), ensure_ascii=True))' $projectPath
if ($LASTEXITCODE -ne 0 -or -not $runtimeJson) { throw 'Cannot read the application runtime directory.' }
$runtimePath = $runtimeJson | ConvertFrom-Json
$recordPath = Join-Path $runtimePath 'process.json'
New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null

$botProcess = $null
if (Test-Path -LiteralPath $recordPath) {
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $candidate = Get-Process -Id $record.processId -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.Path -eq $pythonPath -and
        $candidate.StartTime.ToUniversalTime().Ticks -eq
        ([datetime]$record.startedAt).ToUniversalTime().Ticks) {
        $botProcess = $candidate
    }
}

if ($Action -eq 'stop') {
    if ($botProcess) {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($botProcess.Id)"
        foreach ($child in $children) {
            if ($child.Name -match '^python(w)?\.exe$' -and
                $child.CommandLine -like '*uvicorn*app.main:app*') {
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            }
        }
        Stop-Process -Id $botProcess.Id -ErrorAction SilentlyContinue
        $botProcess.WaitForExit(5000) | Out-Null
    }
    @{ status = 'stopped' } | ConvertTo-Json -Compress
    exit 0
}

if ($Action -eq 'start' -and -not $botProcess) {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        $probe.Connect('127.0.0.1', 8000)
        throw 'Port 8000 is already used by another process. No process was stopped.'
    }
    catch [System.Net.Sockets.SocketException] {
    }
    finally {
        $probe.Dispose()
    }
    $botProcess = Start-Process -FilePath $pythonPath -ArgumentList @(
        '-B', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000',
        '--workers', '1', '--no-access-log'
    ) -WorkingDirectory $projectPath -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $runtimePath 'server.stdout.log') `
      -RedirectStandardError (Join-Path $runtimePath 'server.stderr.log')
    @{
        processId = $botProcess.Id
        startedAt = $botProcess.StartTime.ToUniversalTime().ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding utf8
    $isReady = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($botProcess.HasExited) { throw 'Bot process stopped during startup. See server.stderr.log in the runtime directory.' }
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health/live' -TimeoutSec 1
            if ($health.status -eq 'alive') { $isReady = $true; break }
        }
        catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $isReady) { throw 'Bot did not become ready. See server.stderr.log in the runtime directory.' }
}

@{
    status = $(if ($botProcess -and -not $botProcess.HasExited) { 'started' } else { 'stopped' })
    processId = $(if ($botProcess) { $botProcess.Id } else { $null })
    url = 'http://127.0.0.1:8000'
} | ConvertTo-Json -Compress
