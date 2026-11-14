param([ValidateSet('start', 'stop', 'status')][string]$Action = 'start')
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker is not installed. Install Docker Desktop and start it first.'
    }
    docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop first.' }
    docker compose version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose is required.' }
    if ($Action -eq 'stop') {
        docker compose stop
        if ($LASTEXITCODE -ne 0) { throw 'Cannot stop the project.' }
        Write-Host 'Stopped. Database data is preserved.'
        return
    }
    if ($Action -eq 'status') {
        docker compose ps
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read project status.' }
        return
    }
    Write-Host 'Building application images. The first run can take several minutes...'
    docker compose build
    if ($LASTEXITCODE -ne 0) { throw 'Build failed. Check the Docker output above.' }
    $projectDirectory = (Get-Location).Path
    docker compose run --rm --no-deps --user '0:0' --volume "${projectDirectory}:/workspace" --entrypoint python api /workspace/scripts/prepare_local.py
    if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare .env. Existing settings were not replaced.' }
    docker compose up -d --wait postgres api web
    if ($LASTEXITCODE -ne 0) { throw 'Application did not become ready. Run: docker compose logs api migrate' }
    docker compose exec -T api uv run --no-sync python -m app.cli.bootstrap
    if ($LASTEXITCODE -ne 0) { throw 'Cannot load the initial catalog. Check the output above.' }
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'Cannot start background workers.' }
    Write-Host ''
    Write-Host 'Ready. Default addresses:'
    Write-Host '  Website: http://localhost:8081'
    Write-Host '  Admin:   http://localhost:8081/admin'
    Write-Host '  API:     http://localhost:8000/docs'
    Write-Host 'Admin key: ADMIN_API_TOKEN in .env.'
    Write-Host 'Custom ports, Telegram and AI settings are read from your existing .env.'
    Write-Host 'Without API keys the catalog and ordinary search still work.'
} finally {
    Pop-Location
}
