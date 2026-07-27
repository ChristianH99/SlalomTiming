# Start Slalom Timing on this machine.
#
#   powershell -ExecutionPolicy Bypass -File start.ps1
#   (or just double-click start.bat)
#
# Everything a fresh machine needs, in order: it installs uv if it is missing,
# lets uv fetch Python 3.14 and the dependencies from uv.lock, applies pending
# migrations, offers to create the first operator account (every page requires a
# login) and then runs the server in the foreground. Ctrl+C or closing the
# window stops it.
#
# This is the local/development start. For a real event on a network, configure
# .env and use deploy\start-server.ps1 instead - it runs Daphne with the
# deployment checks. If .env says DJANGO_DEBUG=False this script says so and
# hands over rather than starting a development server on the venue network.
#
# Options:
#   -Port 8000     the port to listen on
#   -Bind 127.0.0.1   the interface (0.0.0.0 = reachable from other devices)
#   -NoBrowser     don't open the browser
#   -SkipSync      skip the dependency/migration checks (a quick restart)

param(
    [int]$Port = 8000,
    [string]$Bind = "127.0.0.1",
    [switch]$NoBrowser,
    [switch]$SkipSync
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

if (-not (Test-Path (Join-Path $root "manage.py"))) {
    throw "manage.py is not next to this script - run it from the project folder."
}

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }

# --- .env, if this machine has one ----------------------------------------
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
}

if ($env:DJANGO_DEBUG -eq "False") {
    Write-Warning "DJANGO_DEBUG=False - this machine is configured for a real deployment."
    Write-Warning "Use:  powershell -ExecutionPolicy Bypass -File deploy\start-server.ps1"
    throw "Refusing to start a development server in a deployment configuration."
}

# --- 1. uv ----------------------------------------------------------------
# uv brings its own Python, so it is the only thing that has to exist first.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Step "uv is not installed - installing it (needs internet, once per machine)"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        throw "Could not install uv automatically: $($_.Exception.Message)`nInstall it manually from https://docs.astral.sh/uv/ and run this script again."
    }
    # The installer puts uv here and updates PATH for *new* shells, not this one.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv was installed but is not on PATH. Close this window, open a new one and run the script again."
    }
}
Write-Host "$(uv --version)" -ForegroundColor DarkGray

if (-not $SkipSync) {
    # --- 2. Python 3.14 + dependencies ------------------------------------
    # Reads .python-version and uv.lock: downloads the interpreter if this
    # machine has no 3.14, and installs exactly the locked package versions.
    # Already up to date = a no-op that costs a second.
    Write-Step "Checking Python and dependencies"
    uv sync
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed - are you online for the first run?" }

    # --- 3. Database ------------------------------------------------------
    # db.sqlite3 is not in the repository, so on a new machine this creates it.
    Write-Step "Applying database migrations"
    uv run python manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw "manage.py migrate failed." }

    # --- 4. The first operator account ------------------------------------
    # Login is required on every page, so a fresh database is unusable without one.
    $probe = "from django.contrib.auth import get_user_model as m; print('HAS_USER' if m().objects.exists() else 'NO_USER')"
    $users = uv run python manage.py shell -c $probe
    if ($LASTEXITCODE -eq 0 -and ($users -join "`n") -match "NO_USER") {
        Write-Host ""
        Write-Host "This database has no user accounts, and every page needs a login." -ForegroundColor Yellow
        Write-Host "Creating the first operator account now (leave the e-mail blank if you like)." -ForegroundColor Yellow
        Write-Host ""
        uv run python manage.py createsuperuser
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "No account was created - you can add one later with: uv run python manage.py createsuperuser"
        }
    }
}

# --- 5. The server --------------------------------------------------------
# One process, always (config/singleinstance.py): the live-update channel layer,
# the CP540 reader thread and its event loop are all per-process. runserver
# serves ASGI/WebSockets itself because Channels is installed.
$url = "http://$(if ($Bind -eq '0.0.0.0') { '127.0.0.1' } else { $Bind }):$Port/"

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        Start-Sleep -Seconds 4
        Start-Process $using:url
    } | Out-Null
}

Write-Host ""
Write-Host "Slalom Timing on $url  -  Ctrl+C to stop" -ForegroundColor Green
Write-Host ""
uv run python manage.py runserver "$($Bind):$Port"
