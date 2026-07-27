# Build the one-click Windows installer for Slalom Timing.
#
#   powershell -ExecutionPolicy Bypass -File build\build.ps1
#
# Produces dist\SlalomTiming-Setup-<version>.exe: a self-contained installer that
# needs nothing on the target machine — no Python, no uv, no internet. It carries
# its own interpreter and every dependency, so what an operator gets is an icon on
# the desktop.
#
# The build machine needs uv and Inno Setup 6; both are installed here if missing
# (winget for Inno Setup). Full description of the pipeline: build\README.md
#
# Options:
#   -Version 0.1.0   override the version from pyproject.toml
#   -NoInstaller     stage and self-test the payload but skip Inno Setup
#   -KeepStage       don't delete build\_stage afterwards (to inspect it)

param(
    [string]$Version,
    [switch]$NoInstaller,
    [switch]$KeepStage
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $PSScriptRoot "_stage"
$dist = Join-Path $root "dist"

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) { throw $text }

# --- 0. Version -----------------------------------------------------------
if (-not $Version) {
    $line = Select-String -Path (Join-Path $root "pyproject.toml") -Pattern '^version\s*=\s*"(.+)"' | Select-Object -First 1
    if (-not $line) { Fail "No version in pyproject.toml - pass -Version instead." }
    $Version = $line.Matches[0].Groups[1].Value
}
Write-Host "Slalom Timing $Version" -ForegroundColor Green

# --- 1. Build-machine tools ----------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Step "Installing uv"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail "uv installed but not on PATH - open a new window and run this again."
    }
}

$iscc = $null
if (-not $NoInstaller) {
    $candidates = @(
        (Get-Command iscc -ErrorAction SilentlyContinue).Source,
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) {
        Write-Step "Installing Inno Setup 6 (winget)"
        winget install --exact --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements --silent
        $iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
        if (-not $iscc) {
            Fail "Inno Setup is still not found. Install it from https://jrsoftware.org/isdl.php and run this again (or use -NoInstaller)."
        }
    }
    Write-Host "Inno Setup: $iscc" -ForegroundColor DarkGray
}

# --- 2. Clean -------------------------------------------------------------
# Nothing in the payload may be a .pyc. They are rebuilt at runtime anyway, they
# roughly double the size of the stdlib, and `__pycache__\<long migration name>.
# cpython-314.pyc` is long enough to push an install path past Windows' 260-char
# limit — which fails Setup halfway through and rolls the whole thing back.
$env:PYTHONDONTWRITEBYTECODE = "1"

Write-Step "Preparing $stage"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
New-Item -ItemType Directory -Path $dist -Force | Out-Null

# --- 3. The interpreter ---------------------------------------------------
# A standalone CPython, unpacked into the payload. This is what makes the result
# installable on a machine with no Python at all.
Write-Step "Fetching Python (standalone build)"
$pyHome = Join-Path $stage "python"
uv python install 3.14 --install-dir $pyHome --no-bin
if ($LASTEXITCODE -ne 0) { Fail "uv python install failed." }

# uv unpacks into a versioned subdirectory (cpython-3.14.x-windows-x86_64-none);
# flatten it so the installed layout doesn't carry a version in a path.
$found = Get-ChildItem $pyHome -Recurse -Filter python.exe -File |
    Where-Object { $_.Directory.Name -ne "Scripts" -and $_.Directory.Name -ne "bin" } |
    Select-Object -First 1
if (-not $found) { Fail "No python.exe under $pyHome" }
if ($found.Directory.FullName -ne (Resolve-Path $pyHome).Path) {
    $inner = $found.Directory.FullName
    Get-ChildItem $inner -Force | ForEach-Object { Move-Item $_.FullName $pyHome -Force }
    Get-ChildItem $pyHome -Directory | Where-Object { $_.Name -like "cpython-*" } |
        Remove-Item -Recurse -Force
}
$py = Join-Path $pyHome "python.exe"
& $py -c "import sys; print(sys.version)"
if ($LASTEXITCODE -ne 0) { Fail "The staged interpreter does not run." }

# uv marks its managed interpreters externally-managed (PEP 668) so nobody pip-installs
# into the one their projects share. This copy is not shared: it is a private payload
# that exists only to carry this app's dependencies, which is exactly the case the
# marker is not aimed at.
$marker = Join-Path $pyHome "Lib\EXTERNALLY-MANAGED"
if (Test-Path $marker) { Remove-Item $marker -Force }

# --- 4. The dependencies --------------------------------------------------
# Straight from uv.lock, so the packaged app is the versions the tests ran
# against - not whatever PyPI serves on build day. The exported file carries
# hashes, which puts pip in --require-hashes mode.
Write-Step "Vendoring dependencies from uv.lock"
$req = Join-Path $stage "requirements.txt"
Push-Location $root
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file $req --quiet
Pop-Location
if ($LASTEXITCODE -ne 0) { Fail "uv export failed." }

& $py -m pip install --disable-pip-version-check --no-warn-script-location --no-input --no-compile -r $req
if ($LASTEXITCODE -ne 0) { Fail "pip install into the payload failed." }
Remove-Item $req -Force

# --- 5. The application ---------------------------------------------------
Write-Step "Copying the application"
$app = Join-Path $stage "app"
New-Item -ItemType Directory -Path $app | Out-Null
foreach ($item in @("manage.py", "config", "apps", "templates", "static", "locale",
                    ".env.example", "DEPLOYMENT.md", "README.md")) {
    $source = Join-Path $root $item
    if (-not (Test-Path $source)) { Fail "Missing from the checkout: $item" }
    Copy-Item $source (Join-Path $app $item) -Recurse -Force
}
Copy-Item (Join-Path $PSScriptRoot "launcher.py") $app -Force
# Never ship a developer's working state: caches, a local event database, tests.
Get-ChildItem $app -Recurse -File -Include "tests.py", "db.sqlite3" -Force |
    Remove-Item -Force -ErrorAction SilentlyContinue

# --- 6. Static files ------------------------------------------------------
# With DEBUG off, WhiteNoise serves STATIC_ROOT and nothing else serves /static/,
# so this has to happen at build time - the installed copy is read-only.
Write-Step "Collecting static files"
$buildData = Join-Path $stage "_builddata"
$env:SLALOM_DATA_DIR = $buildData
$env:DJANGO_STATIC_ROOT = Join-Path $app "staticfiles"
$env:DJANGO_DEBUG = "False"
$env:DJANGO_SECRET_KEY = "build-time-only-not-shipped"
$env:DJANGO_ALLOW_PLAIN_HTTP = "True"
& $py (Join-Path $app "manage.py") collectstatic --noinput --clear | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "collectstatic failed." }

# --- 7. Icon --------------------------------------------------------------
Write-Step "Drawing the icon"
& $py (Join-Path $PSScriptRoot "make_icon.py") (Join-Path $stage "SlalomTiming.ico")
if ($LASTEXITCODE -ne 0) { Fail "make_icon.py failed." }

# --- 8. Slim down ---------------------------------------------------------
# ~60 MB of stdlib test suites, Tk and pip that a timing laptop will never open.
Write-Step "Pruning the payload"
foreach ($junk in @("Lib\test", "Lib\idlelib", "Lib\tkinter", "Lib\turtledemo",
                    "Lib\site-packages\pip", "Lib\site-packages\setuptools",
                    "Lib\site-packages\pkg_resources", "tcl", "Doc", "share")) {
    $path = Join-Path $pyHome $junk
    if (Test-Path $path) { Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue }
}

# The shortcut runs this, so the taskbar and Task Manager say Slalom Timing
# rather than python.exe. A copy, not a rename: pip and the smoke test below
# still expect python.exe to be there.
Copy-Item $py (Join-Path $pyHome "SlalomTiming.exe") -Force

# --- 9. Prove it works ----------------------------------------------------
# The failures that only appear after packaging - a dependency that wasn't
# vendored, a static manifest that never got written - are invisible until
# somebody double-clicks the icon at an event. So the build double-clicks it.
Write-Step "Self-testing the payload"
$testData = Join-Path $stage "_selftest"
$env:SLALOM_DATA_DIR = $testData
Remove-Item Env:\DJANGO_STATIC_ROOT -ErrorAction SilentlyContinue
& (Join-Path $pyHome "SlalomTiming.exe") (Join-Path $app "launcher.py") --selftest
if ($LASTEXITCODE -ne 0) { Fail "The packaged app failed its self-test - not packaging it." }

foreach ($name in @("SLALOM_DATA_DIR", "DJANGO_STATIC_ROOT", "DJANGO_DEBUG",
                    "DJANGO_SECRET_KEY", "DJANGO_ALLOW_PLAIN_HTTP")) {
    Remove-Item "Env:\$name" -ErrorAction SilentlyContinue
}
Remove-Item $testData -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $buildData -Recurse -Force -ErrorAction SilentlyContinue

# Last, because collectstatic and the self-test are themselves Python runs: a
# single .pyc that slipped through is what fails an install at a long path.
Get-ChildItem $stage -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "__pycache__" } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
$leftover = @(Get-ChildItem $stage -Recurse -File -Filter *.pyc -Force)
if ($leftover.Count -gt 0) { Fail "$($leftover.Count) .pyc files are still in the payload." }

$size = "{0:N0} MB" -f ((Get-ChildItem $stage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Payload: $size" -ForegroundColor DarkGray

# --- 10. The installer ----------------------------------------------------
if ($NoInstaller) {
    Write-Host ""
    Write-Host "Payload staged at $stage (-NoInstaller, so no Setup was built)." -ForegroundColor Green
    exit 0
}

Write-Step "Compiling the installer"
& $iscc "/DAppVersion=$Version" "/DStageDir=$stage" "/DOutputDir=$dist" (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup failed." }

if (-not $KeepStage) { Remove-Item $stage -Recurse -Force }

$setup = Join-Path $dist "SlalomTiming-Setup-$Version.exe"
$setupSize = "{0:N0} MB" -f ((Get-Item $setup).Length / 1MB)
Write-Host ""
Write-Host "Built $setup ($setupSize)" -ForegroundColor Green
Write-Host "Copy that one file to the timekeeping laptop and run it." -ForegroundColor Green
