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
    param(
        [string]$Message
    )

    Write-Host "[docker-recovery] $Message"
}

function Get-CommandOutput {
    param(
        [scriptblock]$ScriptBlock
    )

    try {
        return & $ScriptBlock 2>&1 | Out-String
    }
    catch {
        return $_ | Out-String
    }
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

Write-Step 'Stopping stuck Docker Desktop processes if they exist.'
Get-Process 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Write-Step 'Ensuring the Docker Desktop helper service is running.'
$dockerService = Get-Service 'com.docker.service' -ErrorAction Stop
if ($dockerService.Status -ne 'Running') {
    Start-Service -Name 'com.docker.service'
    $dockerService.WaitForStatus('Running', [TimeSpan]::FromSeconds(20))
}

if (-not $SkipDesktopLaunch) {
    if (Test-Path -LiteralPath $dockerDesktopExe) {
        Write-Step 'Launching Docker Desktop.'
        Start-Process -FilePath $dockerDesktopExe | Out-Null
        Start-Sleep -Seconds 8
    }
    else {
        Write-Step "Docker Desktop executable not found at $dockerDesktopExe"
    }
}

Write-Step 'Collecting WSL and Docker access checks.'
$wslStatus = Get-CommandOutput { wsl.exe --status }
$dockerVersion = Get-CommandOutput { docker version }

Write-Host ''
Write-Host 'WSL status result:'
Write-Host $wslStatus.Trim()
Write-Host ''
Write-Host 'Docker version result:'
Write-Host $dockerVersion.Trim()
Write-Host ''

$wslAccessDenied = $wslStatus -match 'E_ACCESSDENIED'
$dockerPermissionDenied = $dockerVersion -match 'permission denied while trying to connect to the docker API'

if ($wslAccessDenied -or $dockerPermissionDenied) {
    Write-Step 'Docker Desktop helper service is running, but this Windows session still cannot access WSL or the Docker named pipe.'
    Write-Step 'Next action: sign out of Windows, sign back in, and then run wsl.exe --status and docker ps in a fresh PowerShell window.'
    Write-Step 'If sign-out does not clear the problem, do a full Windows reboot.'
    exit 1
}

Write-Step 'Recovery checks passed. Docker Desktop should now be usable from this session.'
