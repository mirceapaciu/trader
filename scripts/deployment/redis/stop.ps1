$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$composeFile = Join-Path $scriptDir "docker-compose.yml"

Push-Location $scriptDir
try {
    docker compose -f $composeFile down
}
finally {
    Pop-Location
}
