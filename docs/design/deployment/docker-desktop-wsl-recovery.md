# Docker Desktop WSL Recovery on Windows

## Purpose

Document the local recovery procedure for a Windows sleep/resume failure where
Docker Desktop stays on "Starting the Docker Engine" and normal user-shell Docker
commands stop working.

## Scope

This note covers local Windows development environments that use Docker Desktop
with WSL integration.
It does not cover Linux hosts, remote Docker engines, or production deployment.

## Failure Pattern

Observed on `2026-06-22` after the PC resumed from sleep:

- Docker Desktop UI remained stuck on "Starting the Docker Engine" for more than
  30 minutes
- `wsl.exe --status` returned:
  - `Wsl/EnumerateDistros/Service/E_ACCESSDENIED`
- `docker version` returned:
  - `permission denied while trying to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`
- `com.docker.service` was stopped
- `WSLService` was running
- Docker Desktop UI processes and backend processes were already alive, but the
  engine did not become reachable from the normal user session

## Verified Root Cause Pattern

This failure mode is a Windows session/privilege recovery problem after sleep,
not a normal long-running Docker engine startup.

The key signals were:

- Docker Desktop helper service was not running
- The normal user session could not enumerate WSL distros
- Docker Desktop backend logs initially showed repeated init ping timeouts
- After the helper service was started, Docker engine logs showed successful
  daemon initialization, but the normal user session still could not access WSL
  or the Docker named pipe

This means the engine can be healthy while the current Windows login session is
still broken.

## Useful Checks

Use these commands from PowerShell:

- Service state:
  - `Get-Service com.docker.service,WSLService | Format-Table -Auto Name,Status,StartType`
- WSL access:
  - `wsl.exe --status`
- Docker client access:
  - `docker version`
- Docker Desktop process state:
  - `Get-Process "Docker Desktop","com.docker.backend" -ErrorAction SilentlyContinue | Select-Object Name,Id,StartTime`

Useful logs:

- `%LOCALAPPDATA%\Docker\log\host\monitor.log`
- `%LOCALAPPDATA%\Docker\log\host\com.docker.backend.exe.log`
- `%LOCALAPPDATA%\Docker\log\vm\init.log`

Automation helper:

- `pwsh -File scripts/deployment/docker-desktop/fix-sleep-recovery.ps1`

## Recovery Procedure

### Step 1: Confirm this is the known failure mode

If both of these are true, treat it as the post-sleep WSL/Docker session issue:

- `wsl.exe --status` returns `E_ACCESSDENIED`
- Docker Desktop remains stuck on engine startup

### Step 2: Clear stuck Docker Desktop user processes

Close the wedged user-level Docker Desktop processes:

- `Stop-Process -Name "Docker Desktop","com.docker.backend" -Force -ErrorAction Continue`

This prevents the UI from continuing to loop on stale startup state.

### Step 3: Start the Docker Desktop helper service with elevation

The working fix required a UAC-elevated service start for:

- `com.docker.service`

After this step, Docker Desktop backend logs showed:

- daemon initialization completed
- API listeners became available
- Docker Desktop UI began talking successfully to the VM again

The repository helper script automates:

- stopping stuck `Docker Desktop` and `com.docker.backend` processes
- elevating itself with UAC if needed
- starting `com.docker.service`
- relaunching Docker Desktop
- checking whether the current session can access WSL and Docker again

### Step 4: Re-check normal user access

Run:

- `wsl.exe --status`
- `docker version`

If both now work, recovery is complete.

### Step 5: If WSL still returns `E_ACCESSDENIED`, sign out of Windows

In the observed case, the helper service and engine were healthy after Step 3,
but the current user session still could not:

- enumerate WSL distros
- access `dockerDesktopLinuxEngine`

The remaining fix is:

1. Sign out of Windows
2. Sign back in
3. Open a fresh PowerShell window
4. Re-run:
   - `wsl.exe --status`
   - `docker ps`

If sign-out is not sufficient, do a full Windows reboot.

## What Worked and What Did Not

Worked:

- Killing stuck Docker Desktop user processes
- Starting `com.docker.service` with elevation
- Verifying backend and VM logs instead of trusting the UI spinner

Did not fully fix the normal user session by itself:

- Restarting only Docker Desktop without elevating the helper service
- Relying on the UI state alone
- Checking only whether `WSLService` was running

## Recovery Outcome Interpretation

Use this distinction during troubleshooting:

- If backend logs show successful Docker daemon initialization, the engine is up
- If the normal shell still shows WSL `E_ACCESSDENIED` or named-pipe permission
  errors, the remaining problem is the Windows user session

This distinction prevents unnecessary Docker reinstallation or container cleanup.

## Notes

- Prefer direct service and log checks over Docker Desktop UI status text.
- On this machine, membership in `docker-users` was not sufficient to recover
  the broken post-sleep session without elevated service startup.
- Sign-out is the first user-session reset to try before a full reboot.
