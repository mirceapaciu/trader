$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..\..\..')

Push-Location $repoRoot
try {
    uv run python -m src.product_components.news_fetcher
}
finally {
    Pop-Location
}