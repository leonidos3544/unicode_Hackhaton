param(
    [switch]$Baseline,
    [string]$Cases = 'evals/agent_cases.json',
    [string]$Snapshot = 'data/evaluation/catalog.json',
    [string]$ReferenceTime = '2026-11-14T12:00:00+00:00',
    [string]$Output = 'reports/agent_frozen.json',
    [double]$MaxRub = 10
)
$ErrorActionPreference = 'Stop'
$savedDatabaseUrl = $env:DATABASE_URL
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    docker compose up -d --wait postgres
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL did not start' }
    $exists = docker compose exec -T postgres psql -U bashculture -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'bashculture_agent_eval'"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot check evaluation database' }
    if (($exists -join '').Trim() -ne '1') {
        docker compose exec -T postgres psql -U bashculture -d postgres -c 'CREATE DATABASE bashculture_agent_eval;'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create evaluation database' }
    }
    $evaluationDatabaseUrl = uv run --locked python -c "from app.config import get_settings; from sqlalchemy.engine import make_url; print(make_url(get_settings().database_url).set(database='bashculture_agent_eval').render_as_string(hide_password=False))"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve evaluation database configuration' }
    $env:DATABASE_URL = ($evaluationDatabaseUrl -join '').Trim()
    uv run --locked alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Evaluation migrations failed' }
    $evaluationArgs = @('--frozen-snapshot', $Snapshot, '--reference-time', $ReferenceTime,
        '--cases', $Cases, '--output', $Output, '--max-rub', $MaxRub)
    if ($Baseline) { $evaluationArgs += '--baseline' }
    $evaluationStarted = [DateTime]::UtcNow
    uv run --locked python -m scripts.evaluate_agent @evaluationArgs
    $evaluationExit = $LASTEXITCODE
    if (-not (Test-Path -LiteralPath $Output)) { throw 'Evaluation did not write a report' }
    if ((Get-Item -LiteralPath $Output).LastWriteTimeUtc -lt $evaluationStarted) {
        throw 'Evaluation did not write a fresh report; the previous report was preserved'
    }
    uv run --locked python -m scripts.report_agent_eval $Output --cases $Cases --snapshot ($Output -replace '\.json$', '.catalog.json')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot summarize evaluation' }
    if ($evaluationExit -ne 0) { Write-Warning 'Some evaluation cases failed; see the report.' }
} finally {
    $env:DATABASE_URL = $savedDatabaseUrl
    Pop-Location
}
