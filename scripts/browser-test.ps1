$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
$previousDatabase = $env:DATABASE_URL
$previousTestDatabase = $env:TEST_DATABASE_URL
$previousBrowser = $env:PLAYWRIGHT_CHROMIUM_EXECUTABLE
try {
    if (-not $env:TEST_DATABASE_URL) {
        $testUrl = uv run --locked python -c "from app.config import get_settings; from sqlalchemy.engine import make_url; print(make_url(get_settings().database_url).set(database='bashculture_test').render_as_string(hide_password=False))"
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read test database settings' }
        $env:TEST_DATABASE_URL = ($testUrl -join '').Trim()
    }
    if (-not $env:TEST_DATABASE_URL.EndsWith('/bashculture_test')) { throw 'Only bashculture_test is allowed' }
    $env:DATABASE_URL = $env:TEST_DATABASE_URL
    uv run --locked alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Test migrations failed; run scripts/test.ps1 first' }
    $chromePath = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
    if (-not $env:PLAYWRIGHT_CHROMIUM_EXECUTABLE -and (Test-Path -LiteralPath $chromePath)) {
        $env:PLAYWRIGHT_CHROMIUM_EXECUTABLE = $chromePath
    }
    Push-Location frontend
    try {
        npm.cmd run test:e2e
        if ($LASTEXITCODE -ne 0) { throw 'Browser checks failed' }
    } finally { Pop-Location }
} finally {
    $env:DATABASE_URL = $previousDatabase
    $env:TEST_DATABASE_URL = $previousTestDatabase
    $env:PLAYWRIGHT_CHROMIUM_EXECUTABLE = $previousBrowser
    Pop-Location
}
