$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..\..\..')
$logsDir = Join-Path $repoRoot 'logs'
$backendPidFile = Join-Path $logsDir 'monitoring-ui-backend.pid'
$frontendPidFile = Join-Path $logsDir 'monitoring-ui-frontend.pid'
$backendLog = Join-Path $logsDir 'monitoring-ui-backend.log'
$frontendLog = Join-Path $logsDir 'monitoring-ui-frontend.log'

function Stop-MonitoringProcess {
    param(
        [string]$Name,
        [string]$PidFile,
        [string]$LogFile
    )

    if (-not (Test-Path -LiteralPath $PidFile)) {
        Write-Host "$Name PID file not found: $PidFile"
        return
    }

    $rawPid = (Get-Content -LiteralPath $PidFile -ErrorAction Stop | Select-Object -First 1).Trim()
    if (-not $rawPid) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "$Name PID file was empty and has been removed."
        return
    }

    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $LogFile -Value "[$timestamp] stopping $Name via PID $rawPid"

    cmd.exe /c "taskkill /PID $rawPid /T /F" | Out-Null
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped $Name (PID $rawPid)."
}

Stop-MonitoringProcess -Name 'Monitoring UI backend' -PidFile $backendPidFile -LogFile $backendLog
Stop-MonitoringProcess -Name 'Monitoring UI frontend' -PidFile $frontendPidFile -LogFile $frontendLog
