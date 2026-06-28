param(
    [int]$BackendPort = 8090,
    [int]$FrontendPort = 5174
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..\..\..')
$logsDir = Join-Path $repoRoot 'logs'
$backendPidFile = Join-Path $logsDir 'monitoring-ui-backend.pid'
$frontendPidFile = Join-Path $logsDir 'monitoring-ui-frontend.pid'
$backendLog = Join-Path $logsDir 'monitoring-ui-backend.log'
$frontendLog = Join-Path $logsDir 'monitoring-ui-frontend.log'

function Write-BestEffortLog {
    param(
        [string]$LogFile,
        [string]$Message
    )

    try {
        Out-File -LiteralPath $LogFile -InputObject $Message -Append -Encoding utf8
    }
    catch {
        # Ignore locked log files while shutting down existing processes.
    }
}

function Get-ListeningPid {
    param(
        [int]$Port
    )

    $line = netstat -ano | Select-String "127.0.0.1:$Port\s+.*LISTENING\s+(\d+)" | Select-Object -First 1
    if (-not $line) {
        $line = netstat -ano | Select-String "0.0.0.0:$Port\s+.*LISTENING\s+(\d+)" | Select-Object -First 1
    }
    if (-not $line) {
        return $null
    }

    $parts = ($line.ToString() -split '\s+') | Where-Object { $_ }
    if ($parts.Length -lt 5) {
        return $null
    }
    return $parts[-1]
}

function Stop-MonitoringProcess {
    param(
        [string]$Name,
        [string]$PidFile,
        [string]$LogFile,
        [int]$Port
    )

    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $rawPid = $null

    if (Test-Path -LiteralPath $PidFile) {
        $rawPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    }

    $portPid = Get-ListeningPid -Port $Port
    $pidCandidates = @($rawPid, $portPid) | Where-Object { $_ } | Select-Object -Unique

    if (-not $pidCandidates) {
        if (Test-Path -LiteralPath $PidFile) {
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        }
        Write-Host "$Name is not running."
        return
    }

    foreach ($processId in $pidCandidates) {
        Write-BestEffortLog -LogFile $LogFile -Message "[$timestamp] stopping $Name via PID $processId"
        $taskkillOutput = ""
        try {
            $taskkillOutput = cmd.exe /c "taskkill /PID $processId /T /F" 2>&1 | Out-String
        }
        catch {
            $taskkillOutput = $_.Exception.Message
        }
        if ($LASTEXITCODE -ne 0 -or $taskkillOutput -match 'Access denied') {
            Write-BestEffortLog -LogFile $LogFile -Message "[$timestamp] taskkill issue for $Name PID ${processId}: $taskkillOutput"
        }
    }

    # Wait briefly for the OS to release the port before checking
    Start-Sleep -Milliseconds 500

    $remainingPortPid = Get-ListeningPid -Port $Port
    if ($remainingPortPid -and ($pidCandidates -contains $remainingPortPid)) {
        Write-Host "Failed to stop $Name (PID(s): $remainingPortPid)."
        return
    }

    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped $Name (PID(s): $($pidCandidates -join ', '))."
}

Stop-MonitoringProcess -Name 'Monitoring UI backend' -PidFile $backendPidFile -LogFile $backendLog -Port $BackendPort

# Stop frontend: check the configured port plus a small range in case Vite drifted to another port
$frontendPortsToScan = $FrontendPort..($FrontendPort + 9)
Stop-MonitoringProcess -Name 'Monitoring UI frontend' -PidFile $frontendPidFile -LogFile $frontendLog -Port $FrontendPort
$killedDrifted = $false
foreach ($port in ($frontendPortsToScan | Select-Object -Skip 1)) {
    $driftedPid = Get-ListeningPid -Port $port
    if ($driftedPid) {
        Write-Host "Killing orphaned Vite process on port $port (PID $driftedPid)."
        cmd.exe /c "taskkill /PID $driftedPid /T /F" 2>&1 | Out-Null
        $killedDrifted = $true
    }
}
if ($killedDrifted) {
    Start-Sleep -Milliseconds 500
}
