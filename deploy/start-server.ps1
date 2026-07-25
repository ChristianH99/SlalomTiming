# Start Slalom Timing on the timekeeping laptop (Windows).
#
#   powershell -ExecutionPolicy Bypass -File deploy\start-server.ps1
#
# Reads .env from the project root, applies pending migrations, refreshes the
# collected static files and then runs ONE Daphne process in the foreground.
# Close the window (or Ctrl+C) to stop the server.
#
# Options:
#   -Port 8000        the port to listen on
#   -Bind 0.0.0.0     the interface (0.0.0.0 = reachable from the venue network)
#   -SkipChecks       skip migrate/collectstatic (a restart mid-event)

param(
    [int]$Port = 8000,
    [string]$Bind = "0.0.0.0",
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# --- .env into the process environment ------------------------------------
# settings.py reads os.environ; Django does not load .env itself.
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $name, $value = $line.Split("=", 2)
            [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
        }
    }
    Write-Host "Loaded .env" -ForegroundColor DarkGray
} else {
    Write-Warning "No .env found. Copy .env.example to .env and fill it in before running on a network."
}

if (-not $env:DJANGO_SECRET_KEY) {
    throw "DJANGO_SECRET_KEY is not set (see .env.example). Refusing to start."
}
if ($env:DJANGO_DEBUG -ne "False") {
    Write-Warning "DJANGO_DEBUG is not False - this is a development configuration."
}

# --- release steps --------------------------------------------------------
if (-not $SkipChecks) {
    Write-Host "Checking deployment settings..." -ForegroundColor Cyan
    uv run python manage.py check --deploy
    if ($LASTEXITCODE -ne 0) { throw "manage.py check --deploy failed." }

    Write-Host "Applying database migrations..." -ForegroundColor Cyan
    uv run python manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw "manage.py migrate failed." }

    Write-Host "Collecting static files..." -ForegroundColor Cyan
    uv run python manage.py collectstatic --noinput
    if ($LASTEXITCODE -ne 0) { throw "manage.py collectstatic failed." }
}

# --- the server -----------------------------------------------------------
# One process, always (see config/singleinstance.py): the live-update channel
# layer, the CP540 reader thread and its event loop are all per-process.
Write-Host ""
Write-Host "Slalom Timing on http://$($Bind):$Port/  -  Ctrl+C to stop" -ForegroundColor Green
Write-Host ""
uv run daphne -b $Bind -p $Port config.asgi:application
