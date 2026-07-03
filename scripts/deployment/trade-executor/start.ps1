$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..\..\..')
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$logsDir = Join-Path $repoRoot 'logs'
$logFile = Join-Path $logsDir 'trade-executor.log'
$pidFile = Join-Path $logsDir 'trade-executor.pid'

$null = New-Item -ItemType Directory -Path $logsDir -Force

if (-not (Test-Path -LiteralPath $pythonExe)) {
    Push-Location $repoRoot
    try {
        uv sync
    }
    finally {
        Pop-Location
    }
}

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Out-File -LiteralPath $logFile -InputObject "[$timestamp] starting trade-executor" -Append -Encoding utf8

$command = "& { Set-Location -LiteralPath '$repoRoot'; & '$pythonExe' -m src.product_components.trade_executor }"

$process = Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command) `
    -WindowStyle Minimized `
    -PassThru

Set-Content -LiteralPath $pidFile -Value $process.Id

Write-Host "TradeExecutor starting."
Write-Host "Log: $logFile"
Write-Host "PID file: $pidFile"
Write-Host "Tail log: Get-Content '$logFile' -Wait"
