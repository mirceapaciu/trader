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
    # Give the OS time to release file handles from killed pipeline processes
    Start-Sleep -Milliseconds 500
}

function Write-StartLog {
    param([string]$LogFile, [string]$Message)
    $retries = 5
    for ($i = 0; $i -lt $retries; $i++) {
        try {
            Out-File -LiteralPath $LogFile -InputObject $Message -Append -Encoding utf8
            return
        } catch {
            if ($i -lt $retries - 1) { Start-Sleep -Milliseconds 300 }
        }
    }
    Write-Warning "Could not write to log file: $LogFile"
}

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-StartLog -LogFile $backendLog -Message "[$timestamp] starting backend on $apiBaseUrl"
Write-StartLog -LogFile $frontendLog -Message "[$timestamp] starting frontend on $uiUrl"

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
