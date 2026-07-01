param(
    [switch]$SkipDesktopLaunch
)

$ErrorActionPreference = 'Stop'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Step {
    param([string]$Message)
    Write-Host "[docker-recovery] $Message"
}

function Invoke-WithTimeout {
    param([scriptblock]$ScriptBlock, [int]$TimeoutSeconds = 15)
    $job = Start-Job -ScriptBlock $ScriptBlock
    $finished = Wait-Job $job -Timeout $TimeoutSeconds
    if ($finished) {
        $out = Receive-Job $job 2>&1 | Out-String
    } else {
        Stop-Job $job
        $out = "[timed out after ${TimeoutSeconds}s]"
    }
    Remove-Job $job -Force
    return $out
}

$scriptPath = $MyInvocation.MyCommand.Path

if (-not (Test-IsAdministrator)) {
    Write-Step 'Restarting this script with administrator privileges.'
    Start-Process `
        -FilePath 'powershell.exe' `
        -Verb RunAs `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $scriptPath), '-SkipDesktopLaunch:' + $SkipDesktopLaunch.IsPresent.ToString().ToLowerInvariant())
    exit 0
}

$dockerDesktopExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'

# ── 1. Kill stuck Docker Desktop UI and backend processes ────────────────────
Write-Step 'Stopping stuck Docker Desktop processes if they exist.'
Get-Process 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

# ── 2. Terminate the docker-desktop WSL distro so its Linux engine is torn down
#       cleanly. This is the key step that was missing: com.docker.service may
#       be "Running" on the Windows side while the engine inside WSL is dead after
#       a sleep/resume cycle. Terminating the distro forces a clean restart.
Write-Step 'Terminating docker-desktop WSL distro to reset the Linux engine.'
wsl.exe -t docker-desktop      2>$null
wsl.exe -t docker-desktop-data 2>$null
Start-Sleep -Seconds 2

# ── 3. Restart com.docker.service unconditionally (not just start-if-stopped).
#       A restart re-initialises the engine even when the service appears Running.
Write-Step 'Restarting Docker helper service.'
Restart-Service -Name 'com.docker.service' -Force
(Get-Service 'com.docker.service').WaitForStatus('Running', [TimeSpan]::FromSeconds(20))

# ── 4. Launch Docker Desktop GUI (skipped when running as SYSTEM in scheduled task) ─
if (-not $SkipDesktopLaunch) {
    if (Test-Path -LiteralPath $dockerDesktopExe) {
        Write-Step 'Launching Docker Desktop.'
        Start-Process -FilePath $dockerDesktopExe | Out-Null
    } else {
        Write-Step "Docker Desktop executable not found at $dockerDesktopExe"
    }
}

# ── 5. Poll until the Docker engine responds (up to 90 s) ───────────────────
Write-Step 'Waiting for Docker engine to become available (up to 90 s)...'
$engineReady = $false
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    $v = Invoke-WithTimeout { docker version --format '{{.Server.Version}}' } -TimeoutSeconds 8
    if ($v -notmatch 'timed out' -and $v.Trim() -match '^\d+\.\d+') {
        $engineReady = $true
        break
    }
    Start-Sleep -Seconds 5
}

# ── 6. WSL status (timeout-guarded so it cannot hang the script) ─────────────
Write-Step 'Collecting WSL and Docker access checks.'
$wslStatus    = Invoke-WithTimeout { wsl.exe --status }    -TimeoutSeconds 15
$dockerVersion = Invoke-WithTimeout { docker version }     -TimeoutSeconds 10

Write-Host ''
Write-Host 'WSL status result:'
Write-Host $wslStatus.Trim()
Write-Host ''
Write-Host 'Docker version result:'
Write-Host $dockerVersion.Trim()
Write-Host ''

# ── 7. Exit with the correct status ─────────────────────────────────────────
$wslAccessDenied      = $wslStatus    -match 'E_ACCESSDENIED'
$dockerPermissionDenied = $dockerVersion -match 'permission denied while trying to connect to the docker API'
$dockerEngineDown     = $dockerVersion -match '500 Internal Server Error|context deadline exceeded|cannot connect|connection refused'

if ($wslAccessDenied -or $dockerPermissionDenied) {
    Write-Step 'Docker Desktop helper service is running, but this Windows session still cannot access WSL or the Docker named pipe.'
    Write-Step 'Next action: sign out of Windows, sign back in, and then run wsl.exe --status and docker ps in a fresh PowerShell window.'
    Write-Step 'If sign-out does not clear the problem, do a full Windows reboot.'
    exit 1
}

if ($dockerEngineDown -or -not $engineReady) {
    Write-Step 'Docker engine is still not responding after recovery. A full reboot is required.'
    exit 1
}

Write-Step 'Recovery complete. Docker engine is up and responding.'
