# Packaging Slalom Timing for Windows

`build.ps1` turns this checkout into **one file** — `dist\SlalomTiming-Setup-<version>.exe`
— that installs the app on a Windows machine with no Python, no uv, no internet and no
administrator account, and leaves an icon on the desktop.

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

Roughly 3 minutes; ~28 MB out. Options: `-Version 0.2.0` (default: the version in
`pyproject.toml`), `-NoInstaller` (stage and self-test the payload, skip Setup),
`-KeepStage` (leave `build\_stage` to look at).

## What the build machine needs

`uv` and **Inno Setup 6**. Both are installed by the script if missing — uv from
astral.sh, Inno Setup via `winget install JRSoftware.InnoSetup`. Nothing else: the
interpreter that ends up in the installer is downloaded, not the one you develop with.

## What comes out

```
<install>\python\                 standalone CPython 3.14 + every dependency from uv.lock
        \python\SlalomTiming.exe  a copy of python.exe, so the taskbar says the app's name
        \app\                     this repository's code + launcher.py + staticfiles\
        \SlalomTiming.ico
%LOCALAPPDATA%\SlalomTiming\data\ db.sqlite3, media\, .env, run\, timing_unrecorded.log
```

The split is the important part. **The code is disposable and the data is not** — the
next installer replaces `<install>` wholesale, and an uninstall deletes it, while the
data directory is never written to by Setup and never removed by it. `SLALOM_DATA_DIR`
(config/settings.py) is what points the app there; a checkout, which sets nothing, keeps
writing beside the code exactly as before.

## The steps, and why each one is there

1. **Tools** — uv, and Inno Setup unless `-NoInstaller`.
2. **Interpreter** — `uv python install 3.14 --install-dir`, flattened out of its
   versioned folder. uv marks its managed Pythons externally-managed (PEP 668); the
   marker is removed from *this copy*, which is a private payload rather than a shared
   installation.
3. **Dependencies** — `uv export --frozen` piped into `pip install`, so the packaged app
   runs the versions `uv.lock` pins and the test suite ran against, not whatever PyPI
   serves on build day. The export carries hashes, so pip verifies every wheel.
4. **Application** — `manage.py`, `config`, `apps`, `templates`, `static`, `locale`,
   plus `launcher.py`. Tests and any local `db.sqlite3` are dropped.
5. **`collectstatic`** — mandatory, not optional: the installed app runs with `DEBUG`
   off, where WhiteNoise serves `STATIC_ROOT` and nothing else serves `/static/` at all.
   Skip it and every page renders unstyled with dead timing views.
6. **Icon** — drawn by `make_icon.py` from the design-system tokens, so it can't drift
   away from the palette the app is painted in.
7. **Pruning** — stdlib test suites, Tk, pip. Also **every `.pyc`**: they are rebuilt at
   runtime, they roughly double the stdlib's size, and
   `__pycache__\0016_competition_penalties_by_marshal_posts_marshalpost.cpython-314.pyc`
   is long enough to push an install path past Windows' 260-character limit — which
   fails Setup halfway through and rolls the whole installation back. The build fails
   loudly if one survives.
8. **Self-test** — `launcher.py --selftest` against the staged payload: create the
   database, resolve a hashed static URL, `GET /`, import daphne. These are the failures
   that only exist *after* packaging (a dependency that wasn't vendored, a static
   manifest that was never written) and would otherwise surface when somebody
   double-clicks the icon at an event. A failure here means no installer is produced.
9. **Inno Setup** — `installer.iss`.

## What the operator gets

Double-click Setup, then the desktop icon. On first run the launcher generates a secret
key into `.env`, creates the database, asks once for an operator login (every page in
this app requires one), opens the browser and runs a single Daphne process in the
foreground. That console window *is* the server: closing it stops timing.

The default is `127.0.0.1` with `DJANGO_ALLOW_PLAIN_HTTP=True` — one laptop, nothing on
the network, which is the only case DEPLOYMENT.md §3.4 allows without TLS. Letting
marshals' phones in means editing `SLALOM_BIND` and `DJANGO_ALLOWED_HOSTS` in
`%LOCALAPPDATA%\SlalomTiming\data\.env`, and reading that section first — on venue Wi-Fi
without TLS, every password and every timekeeper's session cookie is readable by
anything else on the network.

## On GitHub

`.github/workflows/windows-installer.yml` runs this same pipeline on a
`windows-latest` runner: test suite, then `build.ps1`. It does two different things
depending on why it ran.

**On every push** it builds and attaches the installer to the run page as an artifact
(90 days). Nothing is published — this is the check that packaging still works, caught
by the push that breaks it rather than on release day.

**On a published release** it builds at the *release's* version and uploads the
installer as a release asset, which is the download link to hand out:

```
https://github.com/<owner>/<repo>/releases/latest/download/SlalomTiming-Setup-<version>.exe
```

So cutting a version is: tag it, publish the release, wait ~15 minutes, and the
installer appears under it. There is nothing to build locally and nothing to upload by
hand.

- **The tag is the version.** `v1.2.3` (or `1.2.3`) builds `SlalomTiming-Setup-1.2.3.exe`
  with `1.2.3` as Setup's `AppVersion`, so the file, the release and Windows' installed-
  programs list all say the same number. A tag that isn't a version number fails the
  build; a tag that disagrees with `pyproject.toml` only warns, because by then the
  release exists and refusing would leave it with no installer at all. Bump
  `pyproject.toml` in the commit you tag.
- **Release assets are not git objects.** That is why they, rather than a branch, hold
  the binaries: git keeps every version of every file forever, and a 28 MB Setup .exe
  recompresses differently on each build, so committing them in sequence would add
  ~28 MB of *permanent* repository weight per build. Assets live outside the object
  store, and deleting an old one actually reclaims the space — a history of installers
  costs nothing here.
- **Re-running a release build replaces its asset** (`gh release upload --clobber`)
  rather than failing on the name.

The runner installs uv itself and Inno Setup through chocolatey (`build.ps1`'s own
winget fallback is not dependable on a hosted runner). Nothing else is configured, and
no secret beyond the automatic `GITHUB_TOKEN` is needed.

The installer is unsigned, so the job summary prints its SHA-256: it is the only way
someone can confirm the file they downloaded is the one that run produced.

## Upgrading and uninstalling

Run the new Setup over the old install; the database, logos and settings are untouched.
Setup refuses to overwrite a *running* server (the launcher holds a named mutex), so it
asks the operator to close it first instead of breaking the installation half-way.
Uninstalling removes the program and says where the data still is.

## Notes

- Unsigned. Windows SmartScreen will show "unknown publisher" on the first run of a
  freshly built Setup — "More info" → "Run anyway". A code-signing certificate is the
  only real fix; there is nothing to configure here for it.
- 64-bit only (`ArchitecturesAllowed=x64compatible`).
- The installer defaults to `%LOCALAPPDATA%\Programs\Slalom Timing` and needs no
  administrator; an admin can pick a machine-wide directory in the dialog.
- `build\_stage` and `dist\` are gitignored build output.
