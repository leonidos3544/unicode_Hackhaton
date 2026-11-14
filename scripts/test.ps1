$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
$previousDatabaseUrl = $env:DATABASE_URL
$previousTestDatabaseUrl = $env:TEST_DATABASE_URL
try {
    docker compose up -d --wait postgres
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL did not start' }
    $exists = docker compose exec -T postgres psql -U bashculture -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'bashculture_test'"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot check test database' }
    if (($exists -join '').Trim() -ne '1') {
        docker compose exec -T postgres psql -U bashculture -d postgres -c 'CREATE DATABASE bashculture_test;'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create test database' }
    }
    $testDatabaseUrl = uv run --locked python -c "from app.config import get_settings; from sqlalchemy.engine import make_url; print(make_url(get_settings().database_url).set(database='bashculture_test').render_as_string(hide_password=False))"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read database configuration' }
    $env:DATABASE_URL = ($testDatabaseUrl -join '').Trim()
    $env:TEST_DATABASE_URL = $env:DATABASE_URL
    uv run --locked alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Test migrations failed' }
    uv run --locked alembic check
    if ($LASTEXITCODE -ne 0) { throw 'Models and migrations differ' }
    uv run --locked pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
} finally {
    $env:DATABASE_URL = $previousDatabaseUrl
    $env:TEST_DATABASE_URL = $previousTestDatabaseUrl
    Pop-Location
}
