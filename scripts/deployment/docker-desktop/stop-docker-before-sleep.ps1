# Runs as SYSTEM via Task Scheduler on every sleep entry (Kernel-Power 506/42).
# Terminates the docker-desktop WSL distros before the system suspends so the
# vsock channel is never left mid-handshake across a sleep boundary.
# Keep this script fast — it executes in the pre-suspend window.

$ErrorActionPreference = 'SilentlyContinue'

wsl.exe -t docker-desktop      2>$null
wsl.exe -t docker-desktop-data 2>$null
