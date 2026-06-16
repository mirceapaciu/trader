param(
    [int]$BackendPort = 8090,
    [int]$FrontendPort = 5174
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..\..\..')
$frontendDir = Join-Path $repoRoot 'src\product_components\monitoring_ui\frontend'
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$npmCmd = (Get-Command npm.cmd -ErrorAction Stop).Source
$logsDir = Join-Path $repoRoot 'logs'
$backendLog = Join-Path $logsDir 'monitoring-ui-backend.log'
$frontendLog = Join-Path $logsDir 'monitoring-ui-frontend.log'
$backendPidFile = Join-Path $logsDir 'monitoring-ui-backend.pid'
$frontendPidFile = Join-Path $logsDir 'monitoring-ui-frontend.pid'
$apiBaseUrl = "http://127.0.0.1:$BackendPort"
$uiUrl = "http://127.0.0.1:$FrontendPort"
$stopScript = Join-Path $scriptDir 'stop.ps1'

$null = New-Item -ItemType Directory -Path $logsDir -Force

if (Test-Path -LiteralPath $stopScript) {
    & $stopScript -BackendPort $BackendPort -FrontendPort $FrontendPort
}

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Out-File -LiteralPath $backendLog -InputObject "[$timestamp] starting backend on $apiBaseUrl" -Append -Encoding utf8
Out-File -LiteralPath $frontendLog -InputObject "[$timestamp] starting frontend on $uiUrl" -Append -Encoding utf8

$backendCommand = "& { `$env:UI_PORT='$BackendPort'; Set-Location -LiteralPath '$repoRoot'; & '$pythonExe' -m src.product_components.monitoring_ui.backend 2>&1 | Out-File -LiteralPath '$backendLog' -Append -Encoding utf8 }"

$frontendCommand = "& { `$env:NO_COLOR='1'; `$env:FORCE_COLOR='0'; `$env:VITE_UI_API_BASE_URL='$apiBaseUrl'; Set-Location -LiteralPath '$frontendDir'; if (-not (Test-Path 'node_modules')) { & '$npmCmd' install 2>&1 | Out-File -LiteralPath '$frontendLog' -Append -Encoding utf8 }; & '$npmCmd' run dev -- --host 127.0.0.1 --port $FrontendPort 2>&1 | Out-File -LiteralPath '$frontendLog' -Append -Encoding utf8 }"

$backendProcess = Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $backendCommand) `
    -WindowStyle Minimized `
    -PassThru

$frontendProcess = Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $frontendCommand) `
    -WindowStyle Minimized `
    -PassThru

Set-Content -LiteralPath $backendPidFile -Value $backendProcess.Id
Set-Content -LiteralPath $frontendPidFile -Value $frontendProcess.Id

Write-Host "Monitoring UI backend starting on $apiBaseUrl"
Write-Host "Monitoring UI frontend starting on $uiUrl"
Write-Host "Backend log: $backendLog"
Write-Host "Frontend log: $frontendLog"
Write-Host "Backend PID file: $backendPidFile"
Write-Host "Frontend PID file: $frontendPidFile"
Write-Host "Tail backend log: Get-Content '$backendLog' -Wait"
Write-Host "Tail frontend log: Get-Content '$frontendLog' -Wait"
Write-Host "Stop script: scripts/deployment/monitoring-ui/stop.ps1"
Write-Host "Open $uiUrl in your browser."
