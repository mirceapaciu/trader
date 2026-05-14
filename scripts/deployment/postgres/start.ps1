$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $scriptDir '.env'
$exampleEnvFile = Join-Path $scriptDir '.env.example'

if (-not (Test-Path $envFile)) {
    Copy-Item $exampleEnvFile $envFile
    Write-Host 'Created .env from .env.example. Update POSTGRES_PASSWORD before production use.'
}

docker compose -f (Join-Path $scriptDir 'docker-compose.yml') up -d
