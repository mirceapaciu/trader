$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$composeFile = Join-Path $scriptDir "docker-compose.yml"
$envExample = Join-Path $scriptDir ".env.example"
$envFile = Join-Path $scriptDir ".env"

if (-not (Test-Path $envFile)) {
    Copy-Item $envExample $envFile
    Write-Host "Created .env from .env.example"
}

Push-Location $scriptDir
try {
    docker compose -f $composeFile up -d
    docker compose -f $composeFile ps
}
finally {
    Pop-Location
}
