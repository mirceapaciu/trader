$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

docker compose -f (Join-Path $scriptDir 'docker-compose.yml') down
