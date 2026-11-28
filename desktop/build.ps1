# Builds the CalSync desktop app and its Windows installer.
#
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1          # Rust on Windows
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -Docker  # Rust in Docker (WSL)
#
# Steps: 1) a PyInstaller build of the engine (desktop\sidecar) into desktop\engine-dist,
#        2) the app icons from brand\mark-1024.png, 3) `tauri build`, which makes the NSIS installer.
# Output: desktop\out\CalSync_<version>_x64-setup.exe. Nothing is signed or published.
#
# Needs Python 3.10+ and Node 18+. Then either Rust with the MSVC toolchain, or, with -Docker,
# WSL with Docker. Use -Docker where Windows Smart App Control is on: it blocks the programs Rust
# builds while compiling ("An Application Control policy has blocked this file", os error 4551).
# -Docker compiles for x86_64-pc-windows-gnu with mingw-w64 in a Linux container (desktop\cross).
param(
    [switch]$Docker,       # compile the app in Docker through WSL
    [switch]$SkipEngine,   # reuse desktop\engine-dist from an earlier run
    [switch]$EngineOnly,   # only build the engine
    [switch]$Debug         # a debug build of the app (Windows build only; faster, has a console)
)
$ErrorActionPreference = "Stop"
$Desktop = $PSScriptRoot
$Repo = Split-Path $Desktop -Parent
$Work = Join-Path $Desktop ".build"
$Out = Join-Path $Desktop "out"
$Venv = Join-Path $Work "venv"
$Py = Join-Path $Venv "Scripts\python.exe"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Run($exe) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "$exe $($args -join ' ') failed ($LASTEXITCODE)" }
}

if (-not $SkipEngine) {
    Step "Engine: Python environment"
    if (-not (Test-Path $Py)) {
        New-Item -ItemType Directory -Force $Work | Out-Null
        if (Get-Command py -ErrorAction SilentlyContinue) { Run py -3 -m venv $Venv } else { Run python -m venv $Venv }
    }
    Run $Py -m pip install --quiet --disable-pip-version-check "pyinstaller==6.16.0" "python-dateutil>=2.8" "tzdata>=2024.1"

    Step "Engine: PyInstaller"
    $dist = Join-Path $Desktop "engine-dist"
    if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
    Run $Py -m PyInstaller --noconfirm --clean --onedir --console --log-level WARN `
        --name calsync-engine `
        --distpath $dist `
        --workpath (Join-Path $Work "pyinstaller") `
        --specpath $Work `
        --paths $Repo `
        --hidden-import calsync --hidden-import calsync_web `
        --collect-data tzdata `
        --exclude-module tkinter --exclude-module unittest --exclude-module pydoc `
        --icon (Join-Path $Repo "brand\favicon.ico") `
        (Join-Path $Desktop "sidecar\calsync_desktop.py")

    Step "Engine: check that it runs"
    $exe = Join-Path $dist "calsync-engine\calsync-engine.exe"
    $tmp = Join-Path $Work "selftest-home"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    $env:CALSYNC_HOME = $tmp
    $said = & $exe status | Out-String
    Remove-Item Env:CALSYNC_HOME
    if ($LASTEXITCODE -ne 0 -or $said -notmatch "last run") { throw "calsync-engine didn't run: $said" }
    Write-Host "calsync-engine runs."
}
if ($EngineOnly) { return }
if (-not (Test-Path (Join-Path $Desktop "engine-dist\calsync-engine"))) { throw "No engine build. Run without -SkipEngine." }

Step "App: icons and loading screen"
Push-Location $Desktop
try {
    if (-not (Test-Path "node_modules\@tauri-apps\cli")) { Run npm.cmd install --no-audit --no-fund }
    Copy-Item (Join-Path $Repo "brand\mark.svg") (Join-Path $Desktop "ui\mark.svg") -Force
    Run npx.cmd tauri icon (Join-Path $Repo "brand\mark-1024.png") -o (Join-Path $Desktop "src-tauri\icons") | Out-Null
    # tauri icon also writes Android and iOS sets; the desktop app doesn't use them.
    foreach ($d in "android", "ios") { $p = Join-Path $Desktop "src-tauri\icons\$d"; if (Test-Path $p) { Remove-Item -Recurse -Force $p } }

    New-Item -ItemType Directory -Force $Out | Out-Null
    Get-ChildItem $Out -Filter *.exe -ErrorAction SilentlyContinue | Remove-Item -Force
    if ($Docker) {
        Step "App: tauri build in Docker (x86_64-pc-windows-gnu)"
        $wslRepo = (& wsl.exe wslpath -a ($Repo -replace '\\', '/')).Trim()
        if (-not $wslRepo) { throw "WSL didn't answer. -Docker needs WSL with Docker." }
        Run wsl.exe sh "$wslRepo/desktop/cross/run.sh"
        $tests = Join-Path $Out "calsync-desktop-tests.exe"
        if (Test-Path $tests) {
            Step "App: unit tests, on this PC"
            try {
                Run $tests
            } catch {
                # Smart App Control judges each new unsigned file; it sometimes refuses the test program.
                if ("$_" -notmatch "Application Control") { throw }
                Write-Warning "Smart App Control blocked the test program, so the Rust tests did NOT run on this PC. The installer is still built."
            }
            Remove-Item $tests -ErrorAction SilentlyContinue
        }
    } else {
        Step "App: tauri build"
        if ($Debug) { Run npx.cmd tauri build --debug } else { Run npx.cmd tauri build }
        Step "App: unit tests"
        Run cargo test --manifest-path (Join-Path $Desktop "src-tauri\Cargo.toml") --release
        $kind = if ($Debug) { "debug" } else { "release" }
        Copy-Item (Join-Path $Desktop "src-tauri\target\$kind\bundle\nsis\*.exe") $Out
    }
} finally { Pop-Location }

$installer = Get-ChildItem $Out -Filter *.exe | Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $installer) { throw "No installer in $Out." }
Step "Done"
Write-Host ("Installer: {0} ({1:N1} MB)" -f $installer.FullName, ($installer.Length / 1MB))
